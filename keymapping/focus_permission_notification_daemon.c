#define _GNU_SOURCE

#include <atspi/atspi.h>
#include <gio/gio.h>
#include <glib-unix.h>

#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/types.h>
#include <unistd.h>

#define SOCKET_NAME "ocdeck-permission-focus.sock"
#define MAX_SCAN_ACCESSIBLES 10000

enum FocusTarget {
    TARGET_ALWAYS,
    TARGET_ONCE,
    TARGET_QUESTION,
    TARGET_BODY,
    TARGET_COUNT,
};

static const char *const target_names[TARGET_COUNT] = {
    [TARGET_ALWAYS] = "Always allow",
    [TARGET_ONCE] = "Allow once",
    [TARGET_QUESTION] = "Open question",
    [TARGET_BODY] = "notification body",
};

static AtspiAccessible *targets[TARGET_COUNT];
static AtspiEventListener *event_listener;
static GMainLoop *main_loop;
static GDBusConnection *session_connection;
static int server_fd = -1;
static char socket_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
static gboolean scan_scheduled;
static guint cycle_position;
static guint notification_subscription;

static char *pending_session_name(const char *notification_id)
{
    const char *runtime_dir = g_get_user_runtime_dir();
    if (!runtime_dir)
        return NULL;

    char *state_dir = g_build_filename(runtime_dir, "ocdeck-permissions", NULL);
    GDir *directory = g_dir_open(state_dir, 0, NULL);
    if (!directory) {
        g_free(state_dir);
        return NULL;
    }

    char *result = NULL;
    char *target_filename = NULL;
    const char *prefix = NULL;
    if (notification_id &&
        g_str_has_prefix(notification_id, "opencode-permission-"))
        prefix = "opencode-permission-";
    else if (notification_id &&
             g_str_has_prefix(notification_id, "opencode-question-"))
        prefix = "opencode-question-";
    if (prefix) {
        const char *pid = notification_id + strlen(prefix);
        const char *end = strchr(pid, '-');
        if (!end)
            end = pid + strlen(pid);
        if (end > pid)
            target_filename = g_strdup_printf("%.*s.json", (int)(end - pid), pid);
    }

    const char *filename;
    while (!result && (filename = g_dir_read_name(directory))) {
        if (!g_str_has_suffix(filename, ".json"))
            continue;
        if (target_filename && g_strcmp0(filename, target_filename) != 0)
            continue;

        char *path = g_build_filename(state_dir, filename, NULL);
        char *contents = NULL;
        gsize length = 0;
        if (!g_file_get_contents(path, &contents, &length, NULL)) {
            g_free(path);
            continue;
        }
        g_free(path);

        const char *array = strstr(contents, "\"permissions\":[");
        if (array)
            array += strlen("\"permissions\":[");
        if (!array || *array == ']') {
            array = strstr(contents, "\"questions\":[");
            if (array)
                array += strlen("\"questions\":[");
        }
        if (!array || *array == ']') {
            g_free(contents);
            continue;
        }

        const char *session = strstr(array, "\"sessionID\":\"");
        if (session) {
            session += strlen("\"sessionID\":\"");
            const char *end = strchr(session, '"');
            if (end && end > session) {
                char *session_id = g_strndup(session, end - session);
                result = g_strdup_printf("oc-%s", session_id);
                g_free(session_id);
            }
        }
        g_free(contents);
    }

    g_dir_close(directory);
    g_free(target_filename);
    g_free(state_dir);
    return result;
}

static void focus_pending_terminal(const char *notification_id)
{
    char *session_name = pending_session_name(notification_id);
    if (!session_name || !session_connection) {
        g_free(session_name);
        return;
    }

    g_dbus_connection_call(
        session_connection,
        "org.local.OCDeckSwitch",
        "/org/local/OCDeckSwitch",
        "org.local.OCDeckSwitch",
        "FocusTmux",
        g_variant_new("(s)", session_name),
        G_VARIANT_TYPE("(b)"),
        G_DBUS_CALL_FLAGS_NONE,
        750,
        NULL,
        NULL,
        NULL);
    g_free(session_name);
}

static void on_notification_action(
    GDBusConnection *connection,
    const gchar *sender_name,
    const gchar *object_path,
    const gchar *interface_name,
    const gchar *signal_name,
    GVariant *parameters,
    gpointer user_data)
{
    (void)connection;
    (void)sender_name;
    (void)object_path;
    (void)interface_name;
    (void)signal_name;
    (void)user_data;

    const char *app_id = NULL;
    const char *notification_id = NULL;
    const char *action = NULL;
    GVariant *parameter = NULL;
    GVariant *platform_data = NULL;
    g_variant_get(parameters, "(&s&s&s@av@a{sv})", &app_id,
                  &notification_id, &action, &parameter, &platform_data);
    (void)notification_id;
    if (g_strcmp0(app_id, "org.local.OCDeckSwitch") == 0 &&
        g_strcmp0(action, "opencode.permission.focus") == 0)
        focus_pending_terminal(notification_id);
    g_clear_pointer(&parameter, g_variant_unref);
    g_clear_pointer(&platform_data, g_variant_unref);
}

static int action_index(const char *name)
{
    if (g_strcmp0(name, target_names[TARGET_ALWAYS]) == 0)
        return TARGET_ALWAYS;
    if (g_strcmp0(name, target_names[TARGET_ONCE]) == 0)
        return TARGET_ONCE;
    if (g_strcmp0(name, target_names[TARGET_QUESTION]) == 0)
        return TARGET_QUESTION;
    return -1;
}

static void clear_target(guint index)
{
    if (index == TARGET_BODY) {
        g_clear_object(&targets[TARGET_ALWAYS]);
        g_clear_object(&targets[TARGET_ONCE]);
        g_clear_object(&targets[TARGET_QUESTION]);
        cycle_position = 0;
    }
    g_clear_object(&targets[index]);
}

static void cache_target(guint index, AtspiAccessible *target)
{
    if (targets[index] == target)
        return;

    if (index == TARGET_BODY) {
        clear_target(TARGET_ALWAYS);
        clear_target(TARGET_ONCE);
        clear_target(TARGET_QUESTION);
        cycle_position = 0;
    }
    g_set_object(&targets[index], target);
}

static gboolean is_showing(AtspiAccessible *accessible)
{
    AtspiStateSet *states = atspi_accessible_get_state_set(accessible);
    if (!states)
        return FALSE;

    const gboolean showing =
        atspi_state_set_contains(states, ATSPI_STATE_SHOWING) &&
        !atspi_state_set_contains(states, ATSPI_STATE_DEFUNCT);
    g_object_unref(states);
    return showing;
}

static AtspiAccessible *notification_ancestor(AtspiAccessible *accessible)
{
    AtspiAccessible *current = g_object_ref(accessible);

    for (guint depth = 0; depth < 16; depth++) {
        GError *error = NULL;
        AtspiAccessible *parent = atspi_accessible_get_parent(current, &error);
        g_object_unref(current);
        if (error) {
            g_clear_error(&error);
            g_clear_object(&parent);
            return NULL;
        }
        if (!parent)
            return NULL;

        const AtspiRole role = atspi_accessible_get_role(parent, &error);
        if (error) {
            g_clear_error(&error);
            g_object_unref(parent);
            return NULL;
        }
        if (role == ATSPI_ROLE_NOTIFICATION)
            return parent;
        current = parent;
    }

    g_object_unref(current);
    return NULL;
}

static void cache_if_relevant_target(AtspiAccessible *accessible)
{
    GError *error = NULL;
    const AtspiRole role = atspi_accessible_get_role(accessible, &error);
    if (error) {
        g_clear_error(&error);
        return;
    }
    if (role != ATSPI_ROLE_BUTTON || !is_showing(accessible))
        return;

    char *name = atspi_accessible_get_name(accessible, &error);
    if (error) {
        g_clear_error(&error);
        g_free(name);
        return;
    }

    const int index = action_index(name);
    if (index >= 0) {
        AtspiAccessible *notification = notification_ancestor(accessible);
        if (notification) {
            cache_target(TARGET_BODY, notification);
            g_object_unref(notification);
        }
        cache_target((guint)index, accessible);
    }
    g_free(name);
}

static void on_accessibility_event(AtspiEvent *event, void *user_data)
{
    (void)user_data;
    if (!event)
        return;
    if (!event->source)
        goto out;

    if (!event->detail1) {
        for (guint i = 0; i < TARGET_COUNT; i++) {
            if (targets[i] == event->source)
                clear_target(i);
        }
        goto out;
    }

    cache_if_relevant_target(event->source);

out:
    g_boxed_free(ATSPI_TYPE_EVENT, event);
}

static void scan_accessible(AtspiAccessible *accessible, guint *visited)
{
    if (!accessible || *visited >= MAX_SCAN_ACCESSIBLES)
        return;
    (*visited)++;

    cache_if_relevant_target(accessible);

    GError *error = NULL;
    const int child_count = atspi_accessible_get_child_count(accessible, &error);
    if (error) {
        g_clear_error(&error);
        return;
    }

    for (int i = 0; i < child_count && *visited < MAX_SCAN_ACCESSIBLES; i++) {
        AtspiAccessible *child =
            atspi_accessible_get_child_at_index(accessible, i, &error);
        if (error) {
            g_clear_error(&error);
            continue;
        }
        scan_accessible(child, visited);
        g_clear_object(&child);
    }
}

static void scan_visible_shell_buttons(void)
{
    AtspiAccessible *desktop = atspi_get_desktop(0);
    if (!desktop)
        return;

    GError *error = NULL;
    const int child_count = atspi_accessible_get_child_count(desktop, &error);
    if (error) {
        g_clear_error(&error);
        return;
    }

    for (int i = 0; i < child_count; i++) {
        AtspiAccessible *application =
            atspi_accessible_get_child_at_index(desktop, i, &error);
        if (error) {
            g_clear_error(&error);
            continue;
        }

        char *name = atspi_accessible_get_name(application, &error);
        if (error)
            g_clear_error(&error);
        if (g_strcmp0(name, "gnome-shell") == 0) {
            guint visited = 0;
            scan_accessible(application, &visited);
            g_free(name);
            g_clear_object(&application);
            return;
        }

        g_free(name);
        g_clear_object(&application);
    }
}

static gboolean focus_target(guint index)
{
    AtspiAccessible *target = targets[index];
    if (!target)
        return FALSE;

    AtspiComponent *component = atspi_accessible_get_component_iface(target);
    if (!component) {
        clear_target(index);
        return FALSE;
    }

    GError *error = NULL;
    const gboolean focused = atspi_component_grab_focus(component, &error);
    g_object_unref(component);
    if (error) {
        g_clear_error(&error);
        clear_target(index);
        return FALSE;
    }
    return focused;
}

static gboolean focus_next_target(void)
{
    guint order[3];
    guint count = 0;

    if (targets[TARGET_ALWAYS] || targets[TARGET_ONCE]) {
        order[count++] = TARGET_ALWAYS;
        order[count++] = TARGET_ONCE;
        order[count++] = TARGET_BODY;
    } else if (targets[TARGET_QUESTION]) {
        order[count++] = TARGET_QUESTION;
        order[count++] = TARGET_BODY;
    } else if (targets[TARGET_BODY]) {
        order[count++] = TARGET_BODY;
    }

    for (guint offset = 0; offset < count; offset++) {
        const guint position = (cycle_position + offset) % count;
        if (focus_target(order[position])) {
            cycle_position = (position + 1) % count;
            return TRUE;
        }
    }
    return FALSE;
}

static gboolean rescan_and_focus(gpointer user_data)
{
    (void)user_data;
    scan_scheduled = FALSE;
    scan_visible_shell_buttons();
    focus_next_target();
    return G_SOURCE_REMOVE;
}

static gboolean initial_scan(gpointer user_data)
{
    (void)user_data;
    scan_scheduled = FALSE;
    scan_visible_shell_buttons();
    return G_SOURCE_REMOVE;
}

static gboolean on_socket_ready(int fd, GIOCondition condition, gpointer user_data)
{
    (void)user_data;
    if (condition & (G_IO_ERR | G_IO_HUP | G_IO_NVAL))
        return G_SOURCE_CONTINUE;

    char buffer[32];
    gboolean requested = FALSE;
    while (recv(fd, buffer, sizeof(buffer), MSG_DONTWAIT) > 0)
        requested = TRUE;

    if (!requested || focus_next_target())
        return G_SOURCE_CONTINUE;

    if (!scan_scheduled) {
        scan_scheduled = TRUE;
        g_idle_add(rescan_and_focus, NULL);
    }
    return G_SOURCE_CONTINUE;
}

static gboolean stop_daemon(gpointer user_data)
{
    (void)user_data;
    if (main_loop)
        g_main_loop_quit(main_loop);
    return G_SOURCE_REMOVE;
}

static gboolean set_accessibility_bus_address(GError **error)
{
    GDBusConnection *connection = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, error);
    if (!connection)
        return FALSE;

    GVariant *result = g_dbus_connection_call_sync(
        connection,
        "org.a11y.Bus",
        "/org/a11y/bus",
        "org.a11y.Bus",
        "GetAddress",
        NULL,
        G_VARIANT_TYPE("(s)"),
        G_DBUS_CALL_FLAGS_NONE,
        2000,
        NULL,
        error);
    if (!result) {
        g_object_unref(connection);
        return FALSE;
    }

    const char *address = NULL;
    g_variant_get(result, "(&s)", &address);
    const gboolean configured = address && g_setenv("AT_SPI_BUS_ADDRESS", address, TRUE);
    g_variant_unref(result);
    if (!configured) {
        g_object_unref(connection);
        return FALSE;
    }
    session_connection = connection;
    return TRUE;
}

static gboolean create_server_socket(void)
{
    const char *runtime_dir = g_get_user_runtime_dir();
    if (!runtime_dir || runtime_dir[0] == '\0')
        return FALSE;
    if (snprintf(socket_path, sizeof(socket_path), "%s/%s", runtime_dir,
                 SOCKET_NAME) >= (int)sizeof(socket_path))
        return FALSE;

    server_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (server_fd < 0)
        return FALSE;

    struct sockaddr_un address = {0};
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, socket_path, strlen(socket_path) + 1);
    const socklen_t length = (socklen_t)(offsetof(struct sockaddr_un, sun_path) +
                                         strlen(address.sun_path) + 1);

    unlink(socket_path);
    const mode_t previous_mask = umask(0077);
    const int bound = bind(server_fd, (const struct sockaddr *)&address, length);
    umask(previous_mask);
    if (bound != 0) {
        close(server_fd);
        server_fd = -1;
        return FALSE;
    }
    return TRUE;
}

int main(void)
{
    GError *error = NULL;
    if (!set_accessibility_bus_address(&error)) {
        g_printerr("Unable to connect to accessibility bus: %s\n",
                   error ? error->message : "unknown error");
        g_clear_error(&error);
        return 1;
    }
    if (atspi_init() != 0) {
        g_printerr("Unable to initialize AT-SPI\n");
        return 1;
    }
    if (!create_server_socket()) {
        g_printerr("Unable to create focus request socket: %s\n", strerror(errno));
        atspi_exit();
        return 1;
    }

    event_listener = atspi_event_listener_new(on_accessibility_event, NULL, NULL);
    if (!event_listener || !atspi_event_listener_register(
            event_listener, "object:state-changed:showing", &error)) {
        g_printerr("Unable to register AT-SPI listener: %s\n",
                   error ? error->message : "unknown error");
        g_clear_error(&error);
        if (event_listener)
            g_object_unref(event_listener);
        close(server_fd);
        unlink(socket_path);
        atspi_exit();
        return 1;
    }

    main_loop = g_main_loop_new(NULL, FALSE);
    notification_subscription = g_dbus_connection_signal_subscribe(
        session_connection,
        "org.gtk.Notifications",
        "org.gtk.Notifications",
        "ActionInvoked",
        "/org/gtk/Notifications",
        NULL,
        G_DBUS_SIGNAL_FLAGS_NONE,
        on_notification_action,
        NULL,
        NULL);
    g_unix_fd_add(server_fd, G_IO_IN | G_IO_ERR | G_IO_HUP | G_IO_NVAL,
                  on_socket_ready, NULL);
    g_unix_signal_add(SIGTERM, stop_daemon, NULL);
    g_unix_signal_add(SIGINT, stop_daemon, NULL);
    scan_scheduled = TRUE;
    g_idle_add(initial_scan, NULL);
    g_main_loop_run(main_loop);

    atspi_event_listener_deregister(
        event_listener, "object:state-changed:showing", NULL);
    g_clear_object(&event_listener);
    if (notification_subscription)
        g_dbus_connection_signal_unsubscribe(session_connection,
                                             notification_subscription);
    for (guint i = 0; i < TARGET_COUNT; i++)
        clear_target(i);
    g_main_loop_unref(main_loop);
    main_loop = NULL;
    close(server_fd);
    server_fd = -1;
    unlink(socket_path);
    atspi_exit();
    g_clear_object(&session_connection);
    return 0;
}
