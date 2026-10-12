from django.urls import path
from . import views

urlpatterns = [
    # Public Search (zero-login)
    path('', views.public_emergency_search, name='public_search'),

    # Staff Dashboard & Requisition Management
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('requests/create/', views.create_emergency_request_view, name='create_emergency_request'),
    path('requests/<int:request_id>/', views.request_detail_view, name='request_detail'),
    path('requests/<int:request_id>/delete/', views.delete_emergency_request_view, name='delete_emergency_request'),

    # Three-Tier Workflow Transitions (POST-only)
    path('requests/<int:request_id>/check-own-stock/', views.check_own_stock_view, name='check_own_stock'),
    path('requests/<int:request_id>/request-bank/', views.request_blood_bank_view, name='request_blood_bank'),
    path('requests/<int:request_id>/accept/', views.accept_bank_request_view, name='accept_bank_request'),
    path('requests/<int:request_id>/dispatch-direct/', views.direct_dispatch_bank_request_view, name='direct_dispatch_bank_request'),
    path('requests/<int:request_id>/reject/', views.reject_bank_request_view, name='reject_bank_request'),
    path('requests/<int:request_id>/issue/', views.issue_blood_view, name='issue_blood'),
    path('requests/<int:request_id>/fallback/', views.trigger_fallback_view, name='trigger_fallback'),

    # Voluntary Donor Fallback
    path('requests/<int:request_id>/donors/', views.donor_directory_view, name='donor_directory'),
    path('requests/<int:request_id>/donors/<int:donor_id>/reveal/', views.reveal_donor_contact_view, name='reveal_donor_contact'),

    # Dispatch Manifest (HTML print-friendly)
    path('requests/<int:request_id>/manifest/', views.dispatch_manifest_view, name='dispatch_manifest'),

    # Inventory Operations & Donations
    path('inventory/add/', views.add_bag_view, name='add_bag'),
    path('inventory/<int:bag_id>/release/', views.release_bag_view, name='release_bag'),
    path('donations/record/', views.record_donation_view, name='record_donation'),

    # CSV Imports
    path('import/bags/', views.csv_import_bags_view, name='csv_import_bags'),
    path('import/donors/', views.csv_import_donors_view, name='csv_import_donors'),
    path('import/template/<str:template_type>/', views.download_csv_template_view, name='download_csv_template'),

    # Organisation Audit Log
    path('audit-log/', views.audit_log_view, name='audit_log'),

    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
]
