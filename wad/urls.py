from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("save-account/", views.save_account, name="save_account"),  # ty: ignore[no-matching-overload]
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
    path("contracts/<uuid:pk>/bulk-book/", views.bulk_book, name="bulk_book"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/clear/", views.clear_time_off, name="clear_time_off"),  # ty: ignore[no-matching-overload]
    path("contracts/<uuid:pk>/export/", views.export_calendar, name="export_calendar"),
    path("contracts/<uuid:pk>/import/", views.import_calendar, name="import_calendar"),  # ty: ignore[no-matching-overload]
    # Calendar subscription
    path("calendar/<str:token>.ics", views.calendar_feed, name="calendar_feed"),
    path("calendar/create-token/", views.create_calendar_token, name="create_calendar_token"),  # ty: ignore[no-matching-overload]
    path("calendar/reset-token/", views.reset_calendar_token, name="reset_calendar_token"),  # ty: ignore[no-matching-overload]
]
