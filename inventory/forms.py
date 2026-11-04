from django import forms

from .models import ReconciliationSession


class SessionCreateForm(forms.ModelForm):
    """Create a session and upload the inventory spreadsheet."""

    expected_file = forms.FileField(
        label="Inventory spreadsheet (.xlsx)",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx"}),
    )

    class Meta:
        model = ReconciliationSession
        fields = ["name", "description", "expected_file"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def clean_expected_file(self):
        f = self.cleaned_data["expected_file"]
        if not f.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("Please upload an .xlsx file.")
        return f


class ColumnMappingForm(forms.Form):
    """Choose which spreadsheet column is the asset tag (match key)."""

    asset_tag_column = forms.ChoiceField(label="Asset-tag column")

    def __init__(self, *args, columns=None, **kwargs):
        super().__init__(*args, **kwargs)
        columns = columns or []
        self.fields["asset_tag_column"].choices = [(c, c) for c in columns]
        # Pre-select a column that looks like an asset tag.
        for c in columns:
            if "asset" in c.lower() and "tag" in c.lower():
                self.fields["asset_tag_column"].initial = c
                break


class ScanForm(forms.Form):
    """Record one scanned/manually-entered device.

    The asset-tag field is always present; the remaining fields are built
    dynamically from the session's value columns so scanned values can be
    diffed against the spreadsheet.
    """

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session
        tag_label = session.asset_tag_column or "Asset tag" if session else "Asset tag"
        self.fields["asset_tag"] = forms.CharField(
            label=tag_label,
            widget=forms.TextInput(attrs={"autofocus": "autofocus"}),
        )
        self.fields["scanned_by"] = forms.CharField(label="Scanned by", required=False)
        self._value_columns = session.value_columns if session else []
        for col in self._value_columns:
            self.fields[f"col__{col}"] = forms.CharField(label=col, required=False)

    def cleaned_data_payload(self):
        """Return (asset_tag, data dict) from the cleaned form."""
        data = {}
        if self.session:
            data[self.session.asset_tag_column] = self.cleaned_data["asset_tag"]
        for col in self._value_columns:
            data[col] = self.cleaned_data.get(f"col__{col}", "")
        return self.cleaned_data["asset_tag"], data
