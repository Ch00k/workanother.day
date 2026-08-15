from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("save-account/", views.save_account, name="save_account"),  # ty: ignore[no-matching-overload]
    path("sellers/", views.seller_list, name="seller_list"),  # ty: ignore[no-matching-overload]
    path("sellers/new/", views.seller_create, name="seller_create"),
    path("sellers/<uuid:pk>/", views.seller_edit, name="seller_edit"),
    path("sellers/<uuid:pk>/delete/", views.seller_delete, name="seller_delete"),  # ty: ignore[no-matching-overload]
    path("buyers/", views.buyer_list, name="buyer_list"),  # ty: ignore[no-matching-overload]
    path("buyers/new/", views.buyer_create, name="buyer_create"),
    path("buyers/<uuid:pk>/", views.buyer_edit, name="buyer_edit"),
    path("buyers/<uuid:pk>/delete/", views.buyer_delete, name="buyer_delete"),  # ty: ignore[no-matching-overload]
    path("contracts/", views.contract_list, name="contract_list"),
    path("contracts/new/", views.contract_create, name="contract_create"),
    path("contracts/<uuid:pk>/", views.calendar_view, name="calendar"),
    path("contracts/<uuid:pk>/edit/", views.contract_edit, name="contract_edit"),
    path("contracts/<uuid:pk>/delete/", views.contract_delete, name="contract_delete"),  # ty: ignore[no-matching-overload]
    # HTMX endpoints
    path("contracts/<uuid:pk>/toggle/<str:date>/", views.toggle_day, name="toggle_day"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/toggle/<str:date>/<str:portion>/", views.toggle_day, name="toggle_day_portion"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/monthly-summary/", views.monthly_summary, name="monthly_summary"),
    path("contracts/<uuid:pk>/holiday-comparison/", views.holiday_comparison, name="holiday_comparison"),
    path("contracts/<uuid:pk>/sync-external/", views.sync_external_calendar, name="sync_external_calendar"),
    path("contracts/<uuid:pk>/bulk-book/", views.bulk_book, name="bulk_book"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/clear/", views.clear_time_off, name="clear_time_off"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/export/", views.export_calendar, name="export_calendar"),
    path("contracts/<uuid:pk>/import/", views.import_calendar, name="import_calendar"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/invoice/<int:year>/<int:month>/", views.invoice_view, name="invoice"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/invoice/<int:year>/<int:month>/send/", views.invoice_send, name="invoice_send"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/invoice/<int:year>/<int:month>/save/", views.invoice_save, name="invoice_save"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/invoices/", views.invoice_list, name="invoice_list"),  # ty: ignore[no-matching-overload]
    path("invoices/<uuid:pk>/", views.invoice_detail, name="invoice_detail"),  # ty: ignore[no-matching-overload]
    path("invoices/<uuid:pk>/send/", views.invoice_send_stored, name="invoice_send_stored"),  # ty: ignore[no-matching-overload]
    path("invoices/<uuid:pk>/delete/", views.invoice_delete, name="invoice_delete"),  # ty: ignore[no-matching-overload]
    path("invoices/<uuid:pk>/issue/", views.invoice_mark_issued, name="invoice_mark_issued"),  # ty: ignore[no-matching-overload]
    path("invoices/<uuid:pk>/edit/", views.invoice_edit, name="invoice_edit"),  # ty: ignore[no-matching-overload]
    path("invoices/<uuid:pk>/status/", views.invoice_status, name="invoice_status"),  # ty: ignore[no-matching-overload]
    # Calendar subscription
    path("calendar/<str:token>.ics", views.calendar_feed, name="calendar_feed"),
    path("calendar/sync/", views.calendar_sync, name="calendar_sync"),  # ty: ignore[no-matching-overload]
    path("calendar/create-token/", views.create_calendar_token, name="create_calendar_token"),  # ty: ignore[no-matching-overload]
    path("calendar/reset-token/", views.reset_calendar_token, name="reset_calendar_token"),  # ty: ignore[no-matching-overload]
]
