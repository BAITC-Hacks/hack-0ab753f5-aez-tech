from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('orders/', views.orders, name='orders'),
    path('data/', views.data_status, name='data_status'),
    path('upload/', views.upload, name='upload'),
    path('run/', views.new_run, name='new_run'),
    path('run/<int:pk>/', views.run_detail, name='run_detail'),
    path('recommendation/<int:pk>/edit/', views.edit_recommendation, name='edit_recommendation'),
    path('run/<int:pk>/approve/', views.approve_run, name='approve_run'),
    path('run/<int:pk>/export/', views.export_run, name='export_run'),
]
