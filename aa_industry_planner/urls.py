from django.urls import path

from . import views

app_name = "aa_industry_planner"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("types/", views.type_search, name="type_search"),
    path("plans/", views.plans, name="plans"),
    path("production-queue/", views.production_queue, name="production_queue"),
    path("plans/archived/remove/", views.delete_archived_plans, name="delete_archived_plans"),
    path("plans/<int:pk>/", views.plan_detail, name="plan"),
    path("plans/<int:pk>/refresh/", views.refresh_plan, name="refresh_plan"),
    path("plans/<int:pk>/remove/", views.delete_archived_plans, name="delete_archived_plan"),
    path("plans/<int:pk>/archive/", views.archive_plan, name="archive_plan"),
    path("plans/<int:pk>/link/", views.link_job, name="link_job"),
    path("plans/<int:pk>/check/<int:item_id>/", views.check_plan_item, name="check_plan_item"),
    path(
        "plans/<int:pk>/purchase/<int:item_id>/", views.set_plan_purchase, name="set_plan_purchase"
    ),
    path("allocations/<int:pk>/unlink/", views.unlink_job, name="unlink_job"),
    path("inventory/", views.inventory, name="inventory"),
    path("inventory/manual/<int:pk>/delete/", views.delete_manual, name="delete_manual"),
    path("sources/<int:pk>/", views.source_action, name="source_action"),
    path("connect/character/", views.connect_character, name="connect_character"),
    path("connect/corporation/", views.connect_corporation, name="connect_corporation"),
    path("connect/structure/", views.connect_structure_lookup, name="connect_structure_lookup"),
    path("jobs/", views.jobs, name="jobs"),
    path("jobs/<int:pk>/assign/", views.assign_job, name="assign_job"),
    path("watches/", views.watches, name="watches"),
    path("watches/<int:pk>/plan/", views.replenish, name="replenish"),
    path("configuration/", views.configuration, name="configuration"),
    path("policies/<int:pk>/delete/", views.delete_policy, name="delete_policy"),
    path("assignments/<int:pk>/delete/", views.delete_assignment, name="delete_assignment"),
    path("storage/<int:pk>/delete/", views.delete_storage_location, name="delete_storage_location"),
    path(
        "storage/<int:location_id>/activities/",
        views.set_activity_storage,
        name="set_activity_storage",
    ),
    path(
        "storage/saved/<int:pk>/delete/",
        views.delete_saved_storage_location,
        name="delete_saved_storage_location",
    ),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/read/", views.mark_read, name="mark_read"),
    path("facilities/", views.facilities, name="facilities"),
    path("facilities/<int:pk>/history/", views.index_history, name="index_history"),
    path("systems/<int:pk>/history/", views.system_index_history, name="system_index_history"),
]
