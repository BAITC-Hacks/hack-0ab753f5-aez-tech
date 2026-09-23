from django import forms

from .importers import KINDS
from .models import Supplier


class UploadForm(forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all(), label='Поставщик')
    kind = forms.ChoiceField(choices=KINDS, label='Тип файла')
    as_of = forms.DateField(label='Дата снимка', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    file = forms.FileField(label='Файл XLSX или CSV')

    def clean_file(self):
        file = self.cleaned_data['file']
        if file.size > 30 * 1024 * 1024:
            raise forms.ValidationError('Файл больше 30 МБ.')
        return file


class RunForm(forms.Form):
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all(), label='Поставщик')
    as_of = forms.DateField(label='Дата расчёта', widget=forms.DateInput(attrs={'type': 'date'}))
    warehouse = forms.CharField(label='Склад', initial='Все', max_length=100)
    category = forms.CharField(label='Категория (пусто = все)', max_length=80, required=False)
    allow_estimated_stock = forms.BooleanField(label='Показать расчёт с начальным остатком месяца (утверждение будет заблокировано)', required=False)
