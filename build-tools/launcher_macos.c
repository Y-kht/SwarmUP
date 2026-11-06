// The program of SwarmUP.app (Contents/MacOS/SwarmUP). It loads the Python of the application for the processor of this Mac inside this very
// process, so macOS sees SwarmUP and not python3: the name in the Dock and the menu, the icon, and the permissions for the folders of the user.
#include <dlfcn.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#if defined(__arm64__) || defined(__aarch64__)
#define PYTHON_FOLDER "python-arm64"
#else
#define PYTHON_FOLDER "python-x86_64"
#endif
#define SCRIPT "app/src/backend/interface/standalone_app.py"
#define LIBRARY "lib/libpython3.12.dylib"
#define MOST_ARGUMENTS 64

typedef int (*PythonMain)(int, char **);

static void fail(const char *text) {
    char script[1024];
    fprintf(stderr, "SwarmUP: %s\n", text);
    snprintf(script, sizeof(script), "display alert \"SwarmUP cannot start\" message \"%s\"", text);
    execl("/usr/bin/osascript", "osascript", "-e", script, (char *)NULL);
    exit(1);
}

// Contents/MacOS/SwarmUP gives Contents/Resources.
static void findResources(char *resources, size_t size) {
    char found[PATH_MAX * 2], real[PATH_MAX];
    uint32_t length = sizeof(found);
    if (_NSGetExecutablePath(found, &length) != 0 || !realpath(found, real))
        fail("It cannot find its own folder.");
    char *last = strrchr(real, '/');
    if (!last)
        fail("It cannot find its own folder.");
    *last = 0;
    last = strrchr(real, '/');
    if (!last)
        fail("It cannot find its own folder.");
    *last = 0;
    snprintf(resources, size, "%s/Resources", real);
}

int main(int argc, char **argv) {
    char resources[PATH_MAX], home[PATH_MAX], library[PATH_MAX], script[PATH_MAX];
    char *arguments[MOST_ARGUMENTS];
    int count = 0;
    findResources(resources, sizeof(resources));
    snprintf(home, sizeof(home), "%s/%s", resources, PYTHON_FOLDER);
    snprintf(library, sizeof(library), "%s/%s", home, LIBRARY);
    snprintf(script, sizeof(script), "%s/%s", resources, SCRIPT);
    setenv("PYTHONHOME", home, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    unsetenv("PYTHONPATH");
    void *python = dlopen(library, RTLD_NOW | RTLD_GLOBAL);
    if (!python)
        fail(dlerror());
    PythonMain start = (PythonMain)dlsym(python, "Py_BytesMain");
    if (!start)
        fail("Its Python has no way to start.");
    arguments[count++] = argv[0];
    arguments[count++] = script;
    // Finder may add the number of the process (-psn_...), which is not for the program.
    for (int index = 1; index < argc && count < MOST_ARGUMENTS - 1; index++)
        if (strncmp(argv[index], "-psn_", 5) != 0)
            arguments[count++] = argv[index];
    arguments[count] = NULL;
    return start(count, arguments);
}
