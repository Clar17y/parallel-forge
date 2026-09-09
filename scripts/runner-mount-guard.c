/* Trusted static entrypoint: no repository code or dynamic loader before proof. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int rejected(void) {
    static const char message[] = "Forge runner mount identity rejected\n";
    (void)write(STDERR_FILENO, message, sizeof(message) - 1);
    return 126;
}

static int identity_number(const char *text, uintmax_t *value) {
    if (!text || !*text) return 0;
    for (const char *cursor = text; *cursor; ++cursor)
        if (*cursor < '0' || *cursor > '9') return 0;
    errno = 0;
    char *end = NULL;
    *value = strtoumax(text, &end, 10);
    return errno == 0 && end && *end == '\0';
}

int main(int argc, char **argv) {
    uintmax_t device, inode;
    if (argc < 5 || strlen(argv[1]) != 36 || !identity_number(argv[2], &device) ||
        !identity_number(argv[3], &inode)) return rejected();
    char boot_id[38];
    int kernel = open("/proc/sys/kernel/random/boot_id", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (kernel < 0) return rejected();
    ssize_t count = read(kernel, boot_id, sizeof(boot_id));
    close(kernel);
    if (count != 37 || boot_id[36] != '\n' || memcmp(boot_id, argv[1], 36) != 0)
        return rejected();
    int directory = open("/workspace", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory < 0) return rejected();
    struct stat actual;
    if (fstat(directory, &actual) != 0 || !S_ISDIR(actual.st_mode) ||
        (uintmax_t)actual.st_dev != device || (uintmax_t)actual.st_ino != inode ||
        fchdir(directory) != 0) {
        close(directory);
        return rejected();
    }
    close(directory);
    execvp(argv[4], &argv[4]);
    return rejected();
}
