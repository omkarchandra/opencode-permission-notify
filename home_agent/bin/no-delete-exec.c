#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/landlock.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

struct allowed_path {
    const char *path;
    bool directory;
};

static int landlock_create_ruleset(const struct landlock_ruleset_attr *attr,
                                   size_t size, uint32_t flags)
{
    return (int)syscall(__NR_landlock_create_ruleset, attr, size, flags);
}

static int landlock_add_path_rule(int ruleset_fd, uint64_t access,
                                  const char *path)
{
    int path_fd = open(path, O_PATH | O_CLOEXEC);
    if (path_fd < 0) {
        fprintf(stderr, "no-delete-exec: cannot open %s: %s\n", path,
                strerror(errno));
        return -1;
    }

    const struct landlock_path_beneath_attr rule = {
        .allowed_access = access,
        .parent_fd = path_fd,
    };
    int result = (int)syscall(__NR_landlock_add_rule, ruleset_fd,
                              LANDLOCK_RULE_PATH_BENEATH, &rule, 0);
    if (result < 0) {
        fprintf(stderr, "no-delete-exec: cannot allow %s: %s\n", path,
                strerror(errno));
    }
    close(path_fd);
    return result;
}

static int landlock_restrict_self(int ruleset_fd)
{
    return (int)syscall(__NR_landlock_restrict_self, ruleset_fd, 0);
}

static void usage(void)
{
    fprintf(stderr,
            "usage: no-delete-exec [--write-root DIR] [--write-file FILE] "
            "-- PROGRAM [ARG ...]\n");
}

int main(int argc, char **argv)
{
    struct allowed_path *paths = calloc((size_t)argc, sizeof(*paths));
    if (paths == NULL) {
        fprintf(stderr, "no-delete-exec: out of memory\n");
        return 125;
    }

    size_t path_count = 0;
    int command_index = 1;
    while (command_index < argc && strcmp(argv[command_index], "--") != 0) {
        bool directory;
        if (strcmp(argv[command_index], "--write-root") == 0) {
            directory = true;
        } else if (strcmp(argv[command_index], "--write-file") == 0) {
            directory = false;
        } else {
            usage();
            free(paths);
            return 125;
        }
        if (++command_index >= argc) {
            usage();
            free(paths);
            return 125;
        }
        paths[path_count++] = (struct allowed_path){
            .path = argv[command_index++],
            .directory = directory,
        };
    }
    if (command_index >= argc - 1 || strcmp(argv[command_index], "--") != 0) {
        usage();
        free(paths);
        return 125;
    }
    command_index++;

    int abi = landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 3) {
        fprintf(stderr,
                "no-delete-exec: Landlock ABI 3 or newer is required for "
                "write-scope enforcement\n");
        free(paths);
        return 125;
    }

    uint64_t file_write = LANDLOCK_ACCESS_FS_WRITE_FILE;
    if (abi >= 3) {
        file_write |= LANDLOCK_ACCESS_FS_TRUNCATE;
    }
    const uint64_t make = LANDLOCK_ACCESS_FS_MAKE_CHAR |
                          LANDLOCK_ACCESS_FS_MAKE_DIR |
                          LANDLOCK_ACCESS_FS_MAKE_REG |
                          LANDLOCK_ACCESS_FS_MAKE_SOCK |
                          LANDLOCK_ACCESS_FS_MAKE_FIFO |
                          LANDLOCK_ACCESS_FS_MAKE_BLOCK |
                          LANDLOCK_ACCESS_FS_MAKE_SYM;
    uint64_t handled = file_write | make |
                       LANDLOCK_ACCESS_FS_REMOVE_DIR |
                       LANDLOCK_ACCESS_FS_REMOVE_FILE;
    if (abi >= 2) {
        handled |= LANDLOCK_ACCESS_FS_REFER;
    }

    const struct landlock_ruleset_attr ruleset = {
        .handled_access_fs = handled,
    };
    int ruleset_fd = landlock_create_ruleset(&ruleset, sizeof(ruleset), 0);
    if (ruleset_fd < 0) {
        fprintf(stderr, "no-delete-exec: cannot create Landlock ruleset: %s\n",
                strerror(errno));
        free(paths);
        return 125;
    }

    for (size_t index = 0; index < path_count; index++) {
        struct stat info;
        if (stat(paths[index].path, &info) < 0) {
            fprintf(stderr, "no-delete-exec: cannot stat %s: %s\n",
                    paths[index].path, strerror(errno));
            close(ruleset_fd);
            free(paths);
            return 125;
        }
        bool is_directory = S_ISDIR(info.st_mode) != 0;
        if (paths[index].directory != is_directory) {
            fprintf(stderr, "no-delete-exec: unexpected path type: %s\n",
                    paths[index].path);
            close(ruleset_fd);
            free(paths);
            return 125;
        }
        uint64_t access = file_write | (paths[index].directory ? make : 0);
        if (landlock_add_path_rule(ruleset_fd, access, paths[index].path) < 0) {
            close(ruleset_fd);
            free(paths);
            return 125;
        }
    }

    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0) {
        fprintf(stderr, "no-delete-exec: cannot set no_new_privs: %s\n",
                strerror(errno));
        close(ruleset_fd);
        free(paths);
        return 125;
    }
    if (landlock_restrict_self(ruleset_fd) < 0) {
        fprintf(stderr, "no-delete-exec: cannot enforce Landlock rules: %s\n",
                strerror(errno));
        close(ruleset_fd);
        free(paths);
        return 125;
    }
    close(ruleset_fd);
    free(paths);

    execvp(argv[command_index], &argv[command_index]);
    fprintf(stderr, "no-delete-exec: cannot execute %s: %s\n",
            argv[command_index], strerror(errno));
    return 126;
}
