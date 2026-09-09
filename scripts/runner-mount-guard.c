/* Trusted static entrypoint: no repository code or dynamic loader before proof. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <dirent.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/xattr.h>
#include <unistd.h>

static int rejected(const char *stage) {
    static const char message[] = "Forge runner mount identity rejected: ";
    (void)write(STDERR_FILENO, message, sizeof(message) - 1);
    (void)write(STDERR_FILENO, stage, strlen(stage));
    (void)write(STDERR_FILENO, "\n", 1);
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

/* The fixed UID can repair its own entries even after a command removed every
 * permission bit.  Traversal starts from the identity-checked root descriptor;
 * lexical repository paths are never reopened. */
struct acl_xattr_header { uint32_t version; };
struct acl_xattr_entry { uint16_t tag; uint16_t perm; uint32_t id; };
#define ACL_XATTR_VERSION 0x0002
#define ACL_USER_OBJ 0x01
#define ACL_USER 0x02
#define ACL_GROUP_OBJ 0x04
#define ACL_MASK 0x10
#define ACL_OTHER 0x20

/* O_PATH lets the fixed UID name an entry it owns even after the command made
 * it mode 000.  fchmodat2(AT_EMPTY_PATH) then restores enough access to open
 * it normally.  The image is linux/amd64, where this syscall number is stable.
 */
#ifndef SYS_fchmodat2
#define SYS_fchmodat2 452
#endif
static int chmod_owned_descriptor(int descriptor, mode_t mode) {
    if (fchmod(descriptor, mode) == 0) return 1;
    if (errno != EBADF) return 0;
    return syscall(SYS_fchmodat2, descriptor, "", mode, AT_EMPTY_PATH) == 0;
}

/* Prove recovery is available before any repository command can remove its
 * own access bits. The probe lives only in the image's private /tmp mount. */
static int recovery_supported(void) {
    char name[] = "/tmp/forge-access-probe-XXXXXX";
    int writable = mkstemp(name);
    if (writable < 0) return 0;
    int descriptor = open(name, O_PATH | O_NOFOLLOW | O_CLOEXEC);
    int ok = descriptor >= 0 && fchmod(writable, 0) == 0;
    unlink(name);
    if (ok) ok = chmod_owned_descriptor(descriptor, S_IRUSR | S_IWUSR);
    if (descriptor >= 0) close(descriptor);
    close(writable);
    return ok;
}

static int repair_acl(int descriptor, uint32_t host_uid, int directory, mode_t original_mode) {
    unsigned char bytes[sizeof(struct acl_xattr_header) + 6 * sizeof(struct acl_xattr_entry)];
    struct acl_xattr_header *header = (struct acl_xattr_header *)bytes;
    struct acl_xattr_entry *entry = (struct acl_xattr_entry *)(header + 1);
    header->version = ACL_XATTR_VERSION;
    uint16_t permissions = directory ? 7 : (uint16_t)(6 | ((original_mode & S_IXUSR) ? 1 : 0));
    uint32_t first = host_uid < 10001 ? host_uid : 10001;
    uint32_t second = host_uid < 10001 ? 10001 : host_uid;
    entry[0] = (struct acl_xattr_entry){ACL_USER_OBJ, permissions, UINT32_MAX};
    entry[1] = (struct acl_xattr_entry){ACL_USER, permissions, first};
    entry[2] = (struct acl_xattr_entry){ACL_USER, permissions, second};
    entry[3] = (struct acl_xattr_entry){ACL_GROUP_OBJ, 0, UINT32_MAX};
    entry[4] = (struct acl_xattr_entry){ACL_MASK, permissions, UINT32_MAX};
    entry[5] = (struct acl_xattr_entry){ACL_OTHER, 0, UINT32_MAX};
    if (fsetxattr(descriptor, "system.posix_acl_access", bytes, sizeof(bytes), 0) != 0) return 0;
    return !directory || fsetxattr(descriptor, "system.posix_acl_default", bytes, sizeof(bytes), 0) == 0;
}

static int same_entry(const struct stat *left, const struct stat *right, int directory) {
    return (directory ? S_ISDIR(right->st_mode) : S_ISREG(right->st_mode)) &&
        right->st_nlink == left->st_nlink && right->st_dev == left->st_dev &&
        right->st_ino == left->st_ino && right->st_uid == left->st_uid;
}

static int repair_tree(int directory, uint32_t host_uid, unsigned depth, unsigned *entries) {
    struct stat metadata;
    if (depth > 128 || ++*entries > 100000 || fstat(directory, &metadata) != 0 ||
        !S_ISDIR(metadata.st_mode) || metadata.st_nlink < 1 ||
        (metadata.st_uid != 10001 && metadata.st_uid != host_uid)) return 0;
    mode_t safe_directory_mode = (metadata.st_mode | S_IRWXU) & ~(S_ISUID | S_ISGID | S_ISVTX);
    if (metadata.st_uid == 10001 && metadata.st_mode != safe_directory_mode &&
        chmod_owned_descriptor(directory, safe_directory_mode) == 0)
        return 0;
    int readable = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (readable < 0) return 0;
    struct stat reopened_directory;
    if (fstat(readable, &reopened_directory) != 0 || !same_entry(&metadata, &reopened_directory, 1)) {
        close(readable);
        return 0;
    }
    if (metadata.st_uid == 10001 && !repair_acl(readable, host_uid, 1, metadata.st_mode)) {
        close(readable);
        return 0;
    }
    int duplicate = dup(readable);
    if (duplicate < 0) { close(readable); return 0; }
    DIR *stream = fdopendir(duplicate);
    if (!stream) { close(duplicate); close(readable); return 0; }
    struct dirent *entry;
    int ok = 1;
    while (ok) {
        errno = 0;
        entry = readdir(stream);
        if (!entry) { if (errno != 0) ok = 0; break; }
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        if (++*entries > 100000) { ok = 0; break; }
        if (fstatat(readable, entry->d_name, &metadata, AT_SYMLINK_NOFOLLOW) != 0) { ok = 0; break; }
        /* Symlink permissions confer no access; never open their targets. */
        if (S_ISLNK(metadata.st_mode)) continue;
        if (
            (metadata.st_uid != host_uid && metadata.st_uid != 10001)) { ok = 0; break; }
        if (S_ISDIR(metadata.st_mode)) {
            int child = openat(readable, entry->d_name, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            struct stat opened;
            if (child < 0 || fstat(child, &opened) != 0 || !same_entry(&metadata, &opened, 1) ||
                !repair_tree(child, host_uid, depth + 1, entries)) ok = 0;
            if (child >= 0) close(child);
        } else if (S_ISREG(metadata.st_mode)) {
            mode_t safe_mode = (metadata.st_mode | S_IRUSR | S_IWUSR) & ~(S_ISUID | S_ISGID | S_ISVTX);
            int child = openat(readable, entry->d_name,
                O_PATH | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
            struct stat opened;
            if (child < 0 || fstat(child, &opened) != 0 || !S_ISREG(opened.st_mode) ||
                opened.st_nlink != 1 || opened.st_dev != metadata.st_dev ||
                opened.st_ino != metadata.st_ino || opened.st_uid != metadata.st_uid ||
                (opened.st_uid == 10001 && opened.st_mode != safe_mode &&
                    chmod_owned_descriptor(child, safe_mode) == 0)) ok = 0;
            if (ok && opened.st_uid == 10001) {
                close(child);
                child = openat(readable, entry->d_name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
                struct stat reopened;
                if (child < 0 || fstat(child, &reopened) != 0 || !same_entry(&opened, &reopened, 0) ||
                    !repair_acl(child, host_uid, 0, opened.st_mode)) ok = 0;
            }
            if (child >= 0) close(child);
        } else ok = 0;
    }
    closedir(stream);
    close(readable);
    return ok;
}

int main(int argc, char **argv) {
    uintmax_t device, inode;
    if (argc < 5 || strlen(argv[1]) != 36 || !identity_number(argv[2], &device) ||
        !identity_number(argv[3], &inode)) return rejected("arguments");
    char boot_id[38];
    int kernel = open("/proc/sys/kernel/random/boot_id", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (kernel < 0) return rejected("kernel-open");
    ssize_t count = read(kernel, boot_id, sizeof(boot_id));
    close(kernel);
    if (count != 37 || boot_id[36] != '\n' || memcmp(boot_id, argv[1], 36) != 0)
        return rejected("kernel-identity");
    int directory = open("/workspace", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory < 0) return rejected("directory-open");
    struct stat actual;
    if (fstat(directory, &actual) != 0 || !S_ISDIR(actual.st_mode) ||
        (uintmax_t)actual.st_dev != device || (uintmax_t)actual.st_ino != inode) {
        close(directory);
        return rejected("directory-identity");
    }
    if (fchdir(directory) != 0) {
        close(directory);
        return rejected("directory-enter");
    }
    if (!recovery_supported()) {
        close(directory);
        return rejected("access-repair-unsupported");
    }
    if (!strcmp(argv[4], "--repair")) {
        uintmax_t host_uid;
        if (argc != 6 || !identity_number(argv[5], &host_uid) || host_uid > UINT32_MAX || host_uid == 10001) {
            close(directory);
            return rejected("arguments");
        }
        unsigned entries = 0;
        int repaired = repair_tree(directory, (uint32_t)host_uid, 0, &entries);
        close(directory);
        return repaired ? 0 : rejected("access-repair");
    }
    close(directory);
    execvp(argv[4], &argv[4]);
    return rejected("command-exec");
}
