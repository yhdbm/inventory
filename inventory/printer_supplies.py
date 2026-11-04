"""On-demand printer supply-level reads.

Self-contained: uses only the standard library (``socket``, ``urllib``) so it
adds no dependency. Most makes are read over SNMP v1 by walking the standard
Printer-MIB supply table (``prtMarkerSupplies``, OID
``1.3.6.1.2.1.43.11.1.1``), which Xerox, LEXMARK and HP devices all populate
with toner / drum / ink levels.

Xerox is the exception: its raw Printer-MIB level/max counters are linear and
were confirmed live to diverge sharply from what the device itself reports
near end-of-life (SNMP math gave 1% for a toner the printer's own embedded
web UI and front panel showed as 10% — Xerox firmware applies an undocumented
non-linear "remaining life" curve the raw counters don't reflect). So Xerox
printers are read by scraping their own embedded status page
(``/stat/consumables.php``, plain HTTP(S), no login) instead of SNMP.

Different makes are handled through a tiny strategy registry: Xerox gets
``XeroxWebConsumablesStrategy``; other laser/toner makes get
``StandardPrinterMibStrategy``; label/thermal makes (Zebra, Sato, Avery)
resolve to ``None`` so callers can skip them entirely.
"""

import re
import socket
import ssl
import urllib.error
import urllib.request

from django.conf import settings


# --- settings with sensible fallbacks --------------------------------------

def _community():
    return getattr(settings, "PRINTER_SNMP_COMMUNITY", "public")


def _timeout():
    return getattr(settings, "PRINTER_SNMP_TIMEOUT", 4)


def _low_threshold():
    return getattr(settings, "PRINTER_SUPPLY_LOW_THRESHOLD", 20)


class SnmpError(Exception):
    """Raised when the device can't be reached or returns no usable data."""


# --- minimal BER / SNMP v1 GETNEXT -----------------------------------------

# Printer-MIB prtMarkerSupplies columns (table index appended on the wire).
OID_SUPPLY_TYPE = "1.3.6.1.2.1.43.11.1.1.5"   # prtMarkerSuppliesType
OID_SUPPLY_DESC = "1.3.6.1.2.1.43.11.1.1.6"   # prtMarkerSuppliesDescription
OID_SUPPLY_MAX = "1.3.6.1.2.1.43.11.1.1.8"    # prtMarkerSuppliesMaxCapacity
OID_SUPPLY_LEVEL = "1.3.6.1.2.1.43.11.1.1.9"  # prtMarkerSuppliesLevel

# prtMarkerSuppliesType values for consumables that genuinely run *low* and so
# warrant a low-% alert: toner(3), ink(5), inkCartridge(6), inkRibbon(7),
# opc/drum(9), developer(10), tonerCartridge(21). Deliberately excludes waste
# receptacles (4 wasteToner, 8 wasteInk) which fill up, and fixed maintenance
# parts (fuser, rollers, transfer belt) that only report a present/OK indicator.
_MEASURABLE_TYPES = {3, 5, 6, 7, 9, 10, 21}


def _enc_len(n):
    if n < 128:
        return bytes([n])
    b = []
    while n:
        b.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(b)]) + bytes(b)


def _tlv(tag, val):
    return bytes([tag]) + _enc_len(len(val)) + val


def _enc_int(n):
    if n == 0:
        return b"\x00"
    b = []
    while n:
        b.insert(0, n & 0xFF)
        n >>= 8
    if b[0] & 0x80:
        b.insert(0, 0)
    return bytes(b)


def _enc_oid(oid):
    parts = [int(x) for x in oid.split(".")]
    body = [40 * parts[0] + parts[1]]
    for p in parts[2:]:
        if p < 128:
            body.append(p)
        else:
            chunk = [p & 0x7F]
            p >>= 7
            while p:
                chunk.insert(0, (p & 0x7F) | 0x80)
                p >>= 7
            body.extend(chunk)
    return bytes(body)


def _rdlen(d, i):
    l = d[i]
    i += 1
    if l & 0x80:
        n = l & 0x7F
        l = int.from_bytes(d[i:i + n], "big")
        i += n
    return l, i


def _dec_oid(b):
    vals = [b[0] // 40, b[0] % 40]
    n = 0
    for c in b[1:]:
        n = (n << 7) | (c & 0x7F)
        if not c & 0x80:
            vals.append(n)
            n = 0
    return ".".join(str(v) for v in vals)


class _Snmp:
    """One SNMP v1 conversation with a host (GETNEXT-based table walk)."""

    def __init__(self, host):
        self.host = host
        self.community = _community().encode()
        self.timeout = _timeout()
        self._reqid = 0

    def _getnext(self, oid):
        self._reqid += 1
        vb = _tlv(0x30, _tlv(0x06, _enc_oid(oid)) + _tlv(0x05, b""))
        pdu = _tlv(
            0xA1,
            _tlv(0x02, _enc_int(self._reqid)) + _tlv(0x02, b"\x00")
            + _tlv(0x02, b"\x00") + _tlv(0x30, vb),
        )
        msg = _tlv(0x30, _tlv(0x02, b"\x00") + _tlv(0x04, self.community) + pdu)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        try:
            s.sendto(msg, (self.host, 161))
            data, _ = s.recvfrom(4096)
        finally:
            s.close()
        return self._parse(data)

    @staticmethod
    def _parse(d):
        i = 0
        _, i = _rdlen(d, i + 1)                 # SEQUENCE
        l, i = _rdlen(d, i + 1)
        i += l                                  # version
        l, i = _rdlen(d, i + 1)
        i += l                                  # community
        _, i = _rdlen(d, i + 1)                 # PDU
        for _ in range(3):                      # request-id, error, error-index
            l, i = _rdlen(d, i + 1)
            i += l
        _, i = _rdlen(d, i + 1)                 # varbind list
        _, i = _rdlen(d, i + 1)                 # varbind
        l, i = _rdlen(d, i + 1)                 # OID
        oid = _dec_oid(d[i:i + l])
        i += l
        vtag = d[i]
        l, i = _rdlen(d, i + 1)
        raw = d[i:i + l]
        if vtag == 0x02:
            # INTEGER is signed in BER/SNMP; Printer-MIB supply level/max use
            # negative sentinels (-1 other, -2 unknown, -3 present-but-unquantified).
            val = int.from_bytes(raw, "big", signed=True)
        elif vtag in (0x41, 0x42, 0x43, 0x44, 0x46):
            val = int.from_bytes(raw, "big")
        elif vtag == 0x04:
            val = raw.decode("latin1", "replace")
        elif vtag == 0x06:
            val = _dec_oid(raw)
        else:
            val = raw.hex()
        return oid, val

    def walk(self, base):
        """Return ``{table_index: value}`` for one column subtree."""
        out = {}
        cur = base
        while True:
            oid, val = self._getnext(cur)
            if not oid.startswith(base + "."):
                break
            out[oid[len(base) + 1:]] = val
            cur = oid
            if len(out) > 60:  # safety backstop
                break
        return out


# Printer-MIB prtMarkerLifeCount (OID .43.10.2.1.4): the marker's lifetime
# impression/page count — i.e. the total impressions meter.
OID_MARKER_LIFE_COUNT = "1.3.6.1.2.1.43.10.2.1.4"


def get_total_impressions(ip):
    """Total lifetime impressions (page count) for a printer, read over SNMP.

    Walks Printer-MIB ``prtMarkerLifeCount`` and returns the largest value found
    — a device normally reports a single marker whose life count is the total
    impressions meter. Raises :class:`SnmpError` on no response / no data.
    """
    snmp = _Snmp(ip)
    try:
        counts = snmp.walk(OID_MARKER_LIFE_COUNT)
    except socket.timeout:
        raise SnmpError("No SNMP response (timed out).")
    except OSError as e:
        raise SnmpError(f"Network error: {e}")
    values = [v for v in counts.values() if isinstance(v, int) and v >= 0]
    if not values:
        raise SnmpError("Printer returned no impression counter over SNMP.")
    return max(values)


# --- supply model ----------------------------------------------------------

class Supply:
    """One consumable line read from a printer."""

    def __init__(self, name, level, maximum, type_code, threshold):
        self.name = _clean_name(name)
        self.level = level
        self.maximum = maximum
        self.type_code = type_code
        if isinstance(level, int) and isinstance(maximum, int) and maximum > 0 and level >= 0:
            self.percent = round(level * 100 / maximum)
        else:
            self.percent = None
        # Only measurable consumables can be "low".
        self.low = (
            self.percent is not None
            and type_code in _MEASURABLE_TYPES
            and self.percent <= threshold
        )

    def status(self):
        if self.percent is not None:
            return f"{self.percent}%"
        if self.level == -2:
            return "ok"
        if self.level == -3:
            return "present"
        return str(self.level)

    def to_dict(self):
        return {
            "name": self.name,
            "level": self.level,
            "max": self.maximum,
            "percent": self.percent,
            "low": self.low,
            "status": self.status(),
        }


def _clean_name(desc):
    """'Black Toner Cartridge, PN 106R04061;SN ...' -> 'Black Toner Cartridge'."""
    name = (desc or "").split(",")[0]
    for sep in (" PN ", " PN", ";"):
        if sep in name:
            name = name.split(sep)[0]
    return name.strip() or "Supply"


# --- strategies ------------------------------------------------------------

class StandardPrinterMibStrategy:
    """Read the standard Printer-MIB supply table. Works for Xerox / LEXMARK / HP."""

    def get_supplies(self, ip):
        snmp = _Snmp(ip)
        threshold = _low_threshold()
        try:
            desc = snmp.walk(OID_SUPPLY_DESC)
            level = snmp.walk(OID_SUPPLY_LEVEL)
            maxc = snmp.walk(OID_SUPPLY_MAX)
            types = snmp.walk(OID_SUPPLY_TYPE)
        except socket.timeout:
            raise SnmpError("No SNMP response (timed out).")
        except OSError as e:
            raise SnmpError(f"Network error: {e}")
        if not desc:
            raise SnmpError("Printer returned no supply data over SNMP.")
        supplies = []
        for idx in sorted(desc, key=lambda x: [int(p) for p in x.split(".")]):
            supplies.append(Supply(
                desc.get(idx, ""),
                level.get(idx),
                maxc.get(idx),
                types.get(idx),
                threshold,
            ).to_dict())
        return supplies


# --- Xerox: read the printer's own calibrated web UI instead of SNMP -------

_XEROX_CONSUMABLES_PATH = "/stat/consumables.php"

# Sections whose supplies can meaningfully run "low" (mirrors _MEASURABLE_TYPES
# for the SNMP path) -- waste containers and fixed maintenance parts are
# excluded even though the page reports a status for them too.
_XEROX_LOW_ELIGIBLE_SECTIONS = {"toner cartridges", "drum cartridges", "ink cartridges"}

# Section heading -> everything up to the next heading (or end of page).
# Deliberately NOT bounded by "the next </table>": some firmware generations
# wrap each row's percent bar in its own nested <table>, whose closing tag
# would end the match after just the first row.  Heading level also varies
# by generation (newer ConnectKey UIs use <h2>, older ones <h3>).
_XEROX_SECTION_RE = re.compile(r"<h[234]>([^<]+)</h[234]>(.*?)(?=<h[234]>|\Z)", re.S)
# Row: icon cell, component name, status word, then whatever else (may hold a
# "NN%" life-remaining figure, may not -- e.g. Waste Toner Container doesn't).
_XEROX_ROW_RE = re.compile(
    r'<tr class="(?:odd|even)">\s*<td>.*?</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>(.*?)</tr>',
    re.S,
)
_XEROX_PERCENT_RE = re.compile(r"(\d+)\s*%")


def _fetch_xerox_consumables_html(ip):
    """GET the printer's own embedded status page. Self-signed cert -- scoped
    to printer LAN IPs only, never a general request."""
    url = f"https://{ip}{_XEROX_CONSUMABLES_PATH}"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "inventory-app"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout(), context=ctx) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise SnmpError(f"Could not reach printer web UI: {e}")


def _parse_xerox_consumables(html):
    supplies = []
    for section_name, table_html in _XEROX_SECTION_RE.findall(html):
        low_eligible = section_name.strip().lower() in _XEROX_LOW_ELIGIBLE_SECTIONS
        for name, status, rest in _XEROX_ROW_RE.findall(table_html):
            name = name.strip() or "Supply"
            status = status.strip() or "unknown"
            m = _XEROX_PERCENT_RE.search(rest)
            percent = int(m.group(1)) if m else None
            supplies.append({
                "name": name,
                "level": None,
                "max": None,
                "percent": percent,
                "low": low_eligible and status.lower() != "ok",
                "status": f"{percent}%" if percent is not None else status,
            })
    return supplies


class XeroxWebConsumablesStrategy:
    """Read Xerox's own calibrated supply percentages from its embedded web
    UI instead of the raw (and, near end-of-life, unreliable) Printer-MIB
    counters -- see module docstring.

    Only newer "ConnectKey" devices (e.g. AltaLink) serve the
    ``/stat/consumables.php`` page this relies on. Older devices (e.g.
    VersaLink C8000, a different embedded-UI generation entirely) can't be
    reached that way, so this falls back to the plain SNMP reading rather
    than reporting the printer as unreadable.
    """

    def get_supplies(self, ip):
        try:
            html = _fetch_xerox_consumables_html(ip)
            supplies = _parse_xerox_consumables(html)
            if not supplies:
                raise SnmpError("Printer web UI returned no supply data.")
            return supplies
        except SnmpError:
            return _DEFAULT_STRATEGY.get_supplies(ip)


# Makes that are label/thermal printers (no toner) -> no level check.
_LABEL_MAKES = {"zebra", "zebra technologies", "sato", "avery"}

_XEROX_MAKES = {"xerox"}

_DEFAULT_STRATEGY = StandardPrinterMibStrategy()
_XEROX_STRATEGY = XeroxWebConsumablesStrategy()


def is_label_make(manufacturer):
    """True when the make is a known label/thermal printer brand.

    Deliberately NOT the same test as ``resolve_strategy(...) is None``, which is
    also None for a *blank* manufacturer — that means "unknown", not "label".
    Callers classifying a printer as label-vs-laser want this.
    """
    return (manufacturer or "").strip().lower() in _LABEL_MAKES


def resolve_strategy(manufacturer, model=""):
    """Return a supply strategy for the given make, or ``None`` if unsupported
    (label/thermal printers, or no manufacturer recorded)."""
    make = (manufacturer or "").strip().lower()
    if not make or make in _LABEL_MAKES:
        return None
    if make in _XEROX_MAKES:
        return _XEROX_STRATEGY
    return _DEFAULT_STRATEGY
