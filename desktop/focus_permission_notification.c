#define _GNU_SOURCE

#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/types.h>
#include <unistd.h>

#define SOCKET_NAME "ocdeck-permission-focus.sock"

int main(void)
{
    const char *runtime_dir = getenv("XDG_RUNTIME_DIR");
    char fallback[64];
    char path[sizeof(((struct sockaddr_un *)0)->sun_path)];

    if (!runtime_dir || runtime_dir[0] == '\0') {
        if (snprintf(fallback, sizeof(fallback), "/run/user/%lu",
                     (unsigned long)getuid()) >= (int)sizeof(fallback))
            return EXIT_FAILURE;
        runtime_dir = fallback;
    }

    if (snprintf(path, sizeof(path), "%s/%s", runtime_dir, SOCKET_NAME) >=
        (int)sizeof(path))
        return EXIT_FAILURE;

    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return EXIT_FAILURE;

    struct sockaddr_un address = {0};
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, path, strlen(path) + 1);

    const char request = 'F';
    const socklen_t length = (socklen_t)(offsetof(struct sockaddr_un, sun_path) +
                                         strlen(address.sun_path) + 1);
    const ssize_t sent = sendto(fd, &request, sizeof(request), MSG_DONTWAIT,
                                (const struct sockaddr *)&address, length);
    close(fd);
    return sent == (ssize_t)sizeof(request) ? EXIT_SUCCESS : EXIT_FAILURE;
}
