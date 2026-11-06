// SwarmUP.exe: this small program with a zip of the application behind it, so the user has one file to run. At its first start it unpacks the zip
// in %LOCALAPPDATA%\SwarmUP\r\<id>, once, and shows a small window meanwhile. Then, like at every next start, it starts the window of SwarmUP with
// the Python of the application. The end of the file says where the zip is: the magic word, its offset, its size and its id (build_executables.py).
#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "miniz.h"

#define TRAILER_SIZE 40
#define MAGIC "SWARMUP1"
#define ID_CHARS 8
#define PATH_CHARS 1024
#define TIMER_ID 1
#define TIMER_MS 150

typedef struct {
    wchar_t exe[PATH_CHARS];
    wchar_t root[PATH_CHARS];
    wchar_t temporary[PATH_CHARS];
    unsigned long long offset, size;
    volatile LONG done, total, finished, failed;
    wchar_t problem[512];
    HWND window;
} Unpack;

static Unpack work;

static void fail(const wchar_t *text) {
    MessageBoxW(NULL, text, L"SwarmUP", MB_OK | MB_ICONERROR);
    ExitProcess(1);
}

static int readTrailer(wchar_t *id) {
    FILE *file = _wfopen(work.exe, L"rb");
    unsigned char trailer[TRAILER_SIZE];
    int good = 0;
    if (!file)
        return 0;
    if (_fseeki64(file, -TRAILER_SIZE, SEEK_END) == 0 && fread(trailer, 1, TRAILER_SIZE, file) == TRAILER_SIZE && memcmp(trailer, MAGIC, 8) == 0) {
        memcpy(&work.offset, trailer + 8, 8);
        memcpy(&work.size, trailer + 16, 8);
        for (int i = 0; i < ID_CHARS; i++)
            id[i] = trailer[24 + i];
        id[ID_CHARS] = 0;
        good = 1;
    }
    fclose(file);
    return good;
}

static int fileExists(const wchar_t *path) {
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES;
}

static void makeFolders(wchar_t *path) {
    for (wchar_t *mark = path + 4; *mark; mark++) {
        if (*mark == L'\\' && mark[-1] != L':') {
            *mark = 0;
            CreateDirectoryW(path, NULL);
            *mark = L'\\';
        }
    }
    CreateDirectoryW(path, NULL);
}

static void setProblem(const wchar_t *text) {
    wcsncpy(work.problem, text, 511);
    InterlockedExchange(&work.failed, 1);
}

// A long path (\\?\) can go deeper than the 260 characters of Windows. The names in the zip are UTF-8.
static int unpackEntry(mz_zip_archive *zip, unsigned int index, const mz_zip_archive_file_stat *stat) {
    wchar_t name[PATH_CHARS], target[PATH_CHARS * 2];
    if (!MultiByteToWideChar(CP_UTF8, 0, stat->m_filename, -1, name, PATH_CHARS))
        return 0;
    for (wchar_t *mark = name; *mark; mark++)
        if (*mark == L'/')
            *mark = L'\\';
    _snwprintf(target, PATH_CHARS * 2, L"\\\\?\\%ls\\%ls", work.temporary, name);
    if (stat->m_is_directory) {
        makeFolders(target);
        return 1;
    }
    wchar_t *slash = wcsrchr(target, L'\\');
    *slash = 0;
    makeFolders(target);
    *slash = L'\\';
    FILE *file = _wfopen(target, L"wb");
    if (!file)
        return 0;
    int good = mz_zip_reader_extract_to_cfile(zip, index, file, 0);
    fclose(file);
    return good;
}

static DWORD WINAPI unpackAll(LPVOID unused) {
    (void)unused;
    FILE *file = _wfopen(work.exe, L"rb");
    mz_zip_archive zip;
    memset(&zip, 0, sizeof(zip));
    if (!file || _fseeki64(file, (long long)work.offset, SEEK_SET) != 0 || !mz_zip_reader_init_cfile(&zip, file, work.size, 0)) {
        setProblem(L"The file of SwarmUP is damaged: download it again.");
    } else {
        unsigned int count = mz_zip_reader_get_num_files(&zip);
        InterlockedExchange(&work.total, (LONG)count);
        for (unsigned int index = 0; index < count && !work.failed; index++) {
            mz_zip_archive_file_stat stat;
            if (!mz_zip_reader_file_stat(&zip, index, &stat) || !unpackEntry(&zip, index, &stat))
                setProblem(L"SwarmUP could not unpack its files. Is the disk full?");
            InterlockedExchange(&work.done, (LONG)index + 1);
        }
        mz_zip_reader_end(&zip);
    }
    if (file)
        fclose(file);
    if (!work.failed) {
        wchar_t ready[PATH_CHARS * 2];
        _snwprintf(ready, PATH_CHARS * 2, L"%ls\\.ready", work.temporary);
        FILE *mark = _wfopen(ready, L"wb");
        if (mark)
            fclose(mark);
        // Another SwarmUP that started at the same time may have finished first: its files are the same.
        if (!MoveFileExW(work.temporary, work.root, MOVEFILE_WRITE_THROUGH) && !fileExists(work.root))
            setProblem(L"SwarmUP could not put its files in place.");
    }
    InterlockedExchange(&work.finished, 1);
    PostMessageW(work.window, WM_NULL, 0, 0);
    return 0;
}

static LRESULT CALLBACK windowProcedure(HWND window, UINT message, WPARAM first, LPARAM second) {
    if (message == WM_TIMER) {
        InvalidateRect(window, NULL, TRUE);
        return 0;
    }
    if (message == WM_PAINT) {
        PAINTSTRUCT paint;
        RECT area, bar, filled;
        wchar_t text[128];
        HDC context = BeginPaint(window, &paint);
        LONG done = work.done, total = work.total;
        int percent = total ? (int)(100LL * done / total) : 0;
        GetClientRect(window, &area);
        _snwprintf(text, 128, L"SwarmUP is getting ready. This is only for its first start: %d%%", percent);
        SetBkMode(context, TRANSPARENT);
        RECT line = {20, 18, area.right - 20, 46};
        DrawTextW(context, text, -1, &line, DT_LEFT | DT_SINGLELINE);
        bar = (RECT){20, 52, area.right - 20, 66};
        FrameRect(context, &bar, (HBRUSH)GetStockObject(GRAY_BRUSH));
        filled = (RECT){21, 53, 21 + (bar.right - bar.left - 2) * percent / 100, 65};
        HBRUSH brush = CreateSolidBrush(RGB(91, 76, 240));
        FillRect(context, &filled, brush);
        DeleteObject(brush);
        EndPaint(window, &paint);
        return 0;
    }
    return DefWindowProcW(window, message, first, second);
}

static void unpackWithWindow(HINSTANCE instance) {
    WNDCLASSW kind;
    memset(&kind, 0, sizeof(kind));
    kind.lpfnWndProc = windowProcedure;
    kind.hInstance = instance;
    kind.lpszClassName = L"SwarmUPSetup";
    kind.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    kind.hCursor = LoadCursor(NULL, IDC_WAIT);
    kind.hIcon = LoadIconW(instance, MAKEINTRESOURCEW(1));
    RegisterClassW(&kind);
    int width = 520, height = 120;
    work.window = CreateWindowExW(0, kind.lpszClassName, L"SwarmUP", WS_OVERLAPPED | WS_CAPTION, (GetSystemMetrics(SM_CXSCREEN) - width) / 2, (GetSystemMetrics(SM_CYSCREEN) - height) / 2,
                                  width, height, NULL, NULL, instance, NULL);
    ShowWindow(work.window, SW_SHOW);
    SetTimer(work.window, TIMER_ID, TIMER_MS, NULL);
    HANDLE thread = CreateThread(NULL, 0, unpackAll, NULL, 0, NULL);
    MSG message;
    while (!work.finished && GetMessageW(&message, NULL, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
    WaitForSingleObject(thread, INFINITE);
    CloseHandle(thread);
    DestroyWindow(work.window);
    if (work.failed)
        fail(work.problem);
}

// What was typed after the name of this program, untouched.
static const wchar_t *argumentsOfThisProgram(void) {
    const wchar_t *line = GetCommandLineW();
    if (*line == L'"') {
        line = wcschr(line + 1, L'"');
        line = line ? line + 1 : L"";
    } else {
        while (*line && *line != L' ' && *line != L'\t')
            line++;
    }
    while (*line == L' ' || *line == L'\t')
        line++;
    return line;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)previous;
    (void)arguments;
    (void)show;
    wchar_t id[ID_CHARS + 1], base[PATH_CHARS], ready[PATH_CHARS * 2], python[PATH_CHARS * 2], command[PATH_CHARS * 4], folder[PATH_CHARS * 2];
    GetModuleFileNameW(NULL, work.exe, PATH_CHARS);
    if (!readTrailer(id))
        fail(L"The file of SwarmUP is damaged: download it again.");
    if (!GetEnvironmentVariableW(L"LOCALAPPDATA", base, PATH_CHARS) && SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, base) != S_OK)
        fail(L"SwarmUP cannot find the folder of your local data.");
    _snwprintf(work.root, PATH_CHARS, L"%ls\\SwarmUP\\r\\%ls", base, id);
    _snwprintf(work.temporary, PATH_CHARS, L"%ls.%lu", work.root, GetCurrentProcessId());
    _snwprintf(ready, PATH_CHARS * 2, L"%ls\\.ready", work.root);
    if (!fileExists(ready)) {
        wchar_t place[PATH_CHARS + 8];
        _snwprintf(place, PATH_CHARS + 8, L"\\\\?\\%ls\\SwarmUP\\r", base);
        makeFolders(place);
        unpackWithWindow(instance);
    }
    _snwprintf(python, PATH_CHARS * 2, L"%ls\\python\\pythonw.exe", work.root);
    _snwprintf(folder, PATH_CHARS * 2, L"%ls\\app", work.root);
    _snwprintf(command, PATH_CHARS * 4, L"\"%ls\" \"%ls\\src\\backend\\interface\\standalone_app.py\" %ls", python, folder, argumentsOfThisProgram());
    STARTUPINFOW start;
    PROCESS_INFORMATION process;
    memset(&start, 0, sizeof(start));
    start.cb = sizeof(start);
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, folder, &start, &process))
        fail(L"SwarmUP could not start its Python. Your antivirus may have blocked it.");
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    CloseHandle(process.hThread);
    return (int)code;
}
