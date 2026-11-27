// OCSub2WB.exe - native Win32 control panel for the local OpenCode Go relay.
//
// The panel never writes config itself: every action goes through the relay's
// loopback /api (line protocol, X-OCGO-Token). The relay keeps running when the
// window closes; only the [退出] button shuts it down.
//
// Build: see build.bat (mingw g++ -municode -mwindows).

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0601

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <windowsx.h>
#include <commctrl.h>
#include <winhttp.h>
#include <shellapi.h>
#include <tlhelp32.h>

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <cwchar>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifndef PBS_SMOOTH
#define PBS_SMOOTH 0x01
#endif
#ifndef NIN_SELECT
#define NIN_SELECT (WM_USER + 0)
#define NIN_KEYSELECT (WM_USER + 1)
#endif
#ifndef NOTIFYICON_VERSION_4
#define NOTIFYICON_VERSION_4 4
#endif

// --------------------------------------------------------------------------- //
// ids / constants
// --------------------------------------------------------------------------- //

enum {
    IDC_STATUS = 1001,
    IDC_BTN_START = 1002,
    IDC_BTN_DIR = 1003,
    IDC_BTN_EXIT = 1004,

    IDC_Q_LABEL1 = 1011, IDC_Q_LABEL2, IDC_Q_LABEL3,
    IDC_Q_BAR1 = 1014, IDC_Q_BAR2, IDC_Q_BAR3,
    IDC_Q_INFO1 = 1017, IDC_Q_INFO2, IDC_Q_INFO3,
    IDC_Q_REFRESH = 1020,
    IDC_RECENT_REQUESTS = 1052,

    IDC_MODELS = 1021,
    IDC_BTN_REFRESH = 1022,
    IDC_BTN_PROBE = 1023,
    IDC_BTN_IMPORT = 1024,
    IDC_HINT = 1025,
    IDC_BTN_SELFTEST = 1026,
    IDC_BTN_PURGE = 1027,
    IDC_BTN_REFRESH_LIST = 1028,

    IDC_KEY = 1031,
    IDC_BTN_KEY = 1032,
    IDC_RADIO_OFF = 1041,
    IDC_RADIO_AUTO = 1042,
    IDC_RADIO_MANUAL = 1043,
    IDC_PROXY_URL = 1044,
    IDC_BTN_PROXY = 1045,

    IDC_MODEL_GROUP = 1029,
    IDC_LOG = 1051
};

static const int CLIENT_W = 1080;
static const int CLIENT_H = 748;
static const DWORD PANEL_STYLE = WS_OVERLAPPEDWINDOW;

static const int WM_APP_HTTP = WM_APP + 1;
static const int WM_APP_EXIT_NOW = WM_APP + 2;
static const int WM_APP_JOB_DONE = WM_APP + 3;
static const int WM_APP_PROXY_CHECK = WM_APP + 4;
static const int WM_APP_TRAY = WM_APP + 5;
static const int WM_APP_SHOW = WM_APP + 6;
static const int WM_APP_FIT_WINDOW = WM_APP + 7;

enum HttpKind {
    K_STATUS = 1,
    K_QUOTA,
    K_QUOTA_DIRECT,
    K_MODELS,
    K_LOG,
    K_ACTION,
    K_PROBE_START,
    K_JOB,
    K_SHUTDOWN
};

// --------------------------------------------------------------------------- //
// globals
// --------------------------------------------------------------------------- //

static HWND g_hwnd = nullptr;
static HFONT g_font = nullptr;
static std::wstring g_exe_dir;
static std::string g_token;
static std::mutex g_token_mutex;
static std::string g_key_masked;
static bool g_running = false;
static bool g_status_busy = false;
static bool g_probe_busy = false;
static std::string g_job_id;
static std::string g_last_probe_summary;
static int g_job_kind = 0;   // 0 probe, 1 selftest
static std::string g_job_model;
static bool g_quota_busy = false;
static bool g_quota_have_last = false;
static std::wstring g_quota_last[3];
static HWND g_tip = nullptr;
static bool g_tip_active = false;
static bool g_hint_sticky = false;
static bool g_action_sticky = false;
static bool g_refresh_notice = false;
static bool g_recovery_prompted = false;
static bool g_restore_pending = false;

static HICON g_icon_large = nullptr;
static HICON g_icon_small = nullptr;
static NOTIFYICONDATAW g_nid = {};
static bool g_tray_added = false;
static bool g_really_exit = false;
static bool g_balloon_shown = false;
static std::wstring g_proxy_mode = L"off";
static std::wstring g_proxy_url_current = L"http://127.0.0.1:7897";
static std::wstring g_status_port = L"8787";
static std::wstring g_tray_tip_last;
static UINT g_taskbar_created = RegisterWindowMessageW(L"TaskbarCreated");

static const int kRelayOnly[] = { IDC_BTN_REFRESH, IDC_BTN_PROBE, IDC_BTN_IMPORT,
                                  IDC_BTN_SELFTEST, IDC_BTN_REFRESH_LIST };

static void SetRelayOnly(bool enabled) {
    for (int id : kRelayOnly) {
        HWND child = GetDlgItem(g_hwnd, id);
        if (child) EnableWindow(child, enabled ? TRUE : FALSE);
    }
}

static void TipActivate(bool on, POINT pt) {
    if (!g_tip) return;
    TOOLINFOW ti = {};
    ti.cbSize = sizeof(ti);
    ti.hwnd = g_hwnd;
    ti.uId = 1;
    if (on) {
        SendMessageW(g_tip, TTM_TRACKACTIVATE, FALSE, (LPARAM)&ti);
        SendMessageW(g_tip, TTM_TRACKPOSITION, 0, MAKELPARAM(pt.x + 12, pt.y - 34));
        SendMessageW(g_tip, TTM_TRACKACTIVATE, TRUE, (LPARAM)&ti);
    } else {
        SendMessageW(g_tip, TTM_TRACKACTIVATE, FALSE, (LPARAM)&ti);
    }
}

static void UpdateRefreshTip() {
    if (!g_tip) return;
    if (GetActiveWindow() != g_hwnd) {
        if (g_tip_active) { POINT none = {0, 0}; TipActivate(false, none); g_tip_active = false; }
        return;
    }
    POINT pt;
    GetCursorPos(&pt);
    bool inside = false;
    for (int id : kRelayOnly) {
        HWND child = GetDlgItem(g_hwnd, id);
        if (!child || IsWindowEnabled(child)) continue;
        RECT rect;
        GetWindowRect(child, &rect);
        if (PtInRect(&rect, pt)) { inside = true; break; }
    }
    if (inside) {
        TipActivate(true, pt);
        g_tip_active = true;
    } else if (g_tip_active) {
        POINT none = {0, 0};
        TipActivate(false, none);
        g_tip_active = false;
    }
}
static std::string g_engine_pid;

struct ModelRow {
    std::string id, name, think, efforts, probe;
    long long ctx = -1, out = -1;
    bool free = false, imported = false;
    bool training = false, limited = false, zdr = false;
};
struct ProbeRow {
    std::string id, route, kind;
    int status = 0;
    int score() const { return status == 200 ? 1 : 0; }
};

static std::wstring U8ToW(const std::string& s);

static std::wstring ThinkLabel(const std::string& think) {
    if (think == "no") return L"无";
    if (think == "always") return L"强制";
    if (think == "toggle") return L"切换";
    return U8ToW(think);
}

// Panel-only label. Never persist "on-off"/"Fixed": WorkBuddy must keep
// receiving an empty/list supportedEfforts from models.json.
static std::wstring EffortsLabel(const ModelRow& m) {
    if (!m.efforts.empty()) return U8ToW(m.efforts);
    if (m.think == "toggle") return L"on-off";
    return L"Fixed";
}

static std::vector<ModelRow> g_models;
static std::vector<ProbeRow> g_probe;
static int g_sort_col = -1;
static bool g_sort_asc = true;
static bool g_proxy_dirty = false;
static bool g_proxy_syncing = false;
static std::string g_proxy_applied;
static std::wstring g_proxy_url_synced = L"http://127.0.0.1:7897";
static bool g_proxy_url_invalid = false;
static bool g_proxy_pending = false;
static HBRUSH g_invalid_brush = nullptr;

struct ProxyCheckResult {
    std::string mode;
    std::string url;
    bool ok = false;
    std::string detail;
};
static LRESULT CALLBACK ModelsSubclass(HWND, UINT, WPARAM, LPARAM, UINT_PTR, DWORD_PTR);

static void SetText(int id, const std::wstring& text);
static void SetHintUser(const std::wstring& text);
static void SetHintAuto(const std::wstring& text);
static void SetHintSticky(const std::wstring& text);
static void UpdateModelGroupTitle(bool failed);
static void ShowPanel();
static void UpdateTrayTip();
static void RefreshStatusAsync();

// --------------------------------------------------------------------------- //
// text helpers
// --------------------------------------------------------------------------- //

static std::wstring U8ToW(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &out[0], n);
    return out;
}

static std::string WToU8(const std::wstring& s) {
    if (s.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0, nullptr, nullptr);
    std::string out(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(), &out[0], n, nullptr, nullptr);
    return out;
}

static std::string Trim(std::string s) {
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n' || s.back() == ' ' || s.back() == '\t'))
        s.pop_back();
    size_t i = 0;
    while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) ++i;
    return s.substr(i);
}

static std::vector<std::string> Split(const std::string& s, char sep) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s) {
        if (c == sep) { out.push_back(cur); cur.clear(); }
        else cur.push_back(c);
    }
    out.push_back(cur);
    return out;
}

static std::vector<std::string> Lines(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s) {
        if (c == '\n') { out.push_back(Trim(cur)); cur.clear(); }
        else cur.push_back(c);
    }
    if (!cur.empty()) out.push_back(Trim(cur));
    return out;
}

static std::map<std::string, std::string> ParseKV(const std::string& line) {
    std::map<std::string, std::string> kv;
    for (const auto& raw : Split(line, ' ')) {
        std::string tok = Trim(raw);
        if (tok.empty()) continue;
        auto pos = tok.find('=');
        if (pos != std::string::npos) kv[tok.substr(0, pos)] = tok.substr(pos + 1);
    }
    return kv;
}

static std::string FileRead(const std::wstring& path) {
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return "";
    DWORD size = GetFileSize(h, nullptr);
    if (size == INVALID_FILE_SIZE || size > 1024 * 1024) { CloseHandle(h); return ""; }
    std::string data(size, '\0');
    DWORD read = 0;
    ReadFile(h, &data[0], size, &read, nullptr);
    CloseHandle(h);
    data.resize(read);
    return Trim(data);
}

static bool LoadToken() {
    std::string t = FileRead(g_exe_dir + L"\\cache\\api.token");
    if (t.empty()) return false;
    {
        std::lock_guard<std::mutex> lock(g_token_mutex);
        g_token = t;
    }
    return true;
}

// --------------------------------------------------------------------------- //
// HTTP (WinHTTP, never through a system proxy)
// --------------------------------------------------------------------------- //

static std::string HttpOnce(const std::wstring& method, const std::wstring& path,
                            const std::string& body, int timeout_ms, int* status_out) {
    *status_out = 0;
    HINTERNET ses = WinHttpOpen(L"ocgo-panel/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                                WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!ses) return "E winhttp-open";
    WinHttpSetTimeouts(ses, 1000, 2000, 3000, timeout_ms);

    HINTERNET con = WinHttpConnect(ses, L"127.0.0.1", 8787, 0);
    if (!con) { WinHttpCloseHandle(ses); return "E winhttp-connect"; }

    HINTERNET req = WinHttpOpenRequest(con, method.c_str(), path.c_str(), nullptr,
                                       WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!req) {
        WinHttpCloseHandle(con); WinHttpCloseHandle(ses);
        return "E winhttp-request";
    }

    DWORD flags = WINHTTP_DISABLE_REDIRECTS;
    WinHttpSetOption(req, WINHTTP_OPTION_DISABLE_FEATURE, &flags, sizeof(flags));

    std::string token;
    {
        std::lock_guard<std::mutex> lock(g_token_mutex);
        token = g_token;
    }
    std::wstring hdr = L"X-OCGO-Token: " + U8ToW(token) + L"\r\n";
    WinHttpAddRequestHeaders(req, hdr.c_str(), (DWORD)-1,
                             WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE);

    BOOL ok;
    if (!body.empty()) {
        ok = WinHttpSendRequest(req, L"Content-Type: application/x-www-form-urlencoded\r\n", (DWORD)-1,
                                (LPVOID)body.data(), (DWORD)body.size(), (DWORD)body.size(), 0);
    } else {
        ok = WinHttpSendRequest(req, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                                WINHTTP_NO_REQUEST_DATA, 0, 0, 0);
    }
    if (!ok) {
        WinHttpCloseHandle(req); WinHttpCloseHandle(con); WinHttpCloseHandle(ses);
        return "E send";
    }
    if (!WinHttpReceiveResponse(req, nullptr)) {
        WinHttpCloseHandle(req); WinHttpCloseHandle(con); WinHttpCloseHandle(ses);
        return "E receive";
    }

    DWORD code = 0, len = sizeof(code);
    WinHttpQueryHeaders(req, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                        WINHTTP_HEADER_NAME_BY_INDEX, &code, &len, WINHTTP_NO_HEADER_INDEX);
    *status_out = (int)code;

    std::string out;
    DWORD avail = 0;
    while (WinHttpQueryDataAvailable(req, &avail) && avail > 0) {
        std::string buf(avail, '\0');
        DWORD read = 0;
        if (!WinHttpReadData(req, &buf[0], avail, &read)) break;
        buf.resize(read);
        out += buf;
        if (out.size() > 4 * 1024 * 1024) break;
    }
    WinHttpCloseHandle(req);
    WinHttpCloseHandle(con);
    WinHttpCloseHandle(ses);
    return out;
}

static std::string HttpCall(const std::wstring& method, const std::wstring& path,
                            const std::string& body, int timeout_ms, int* status_out) {
    std::string out = HttpOnce(method, path, body, timeout_ms, status_out);
    if (*status_out == 401) {
        // The relay was restarted and rotated its token: reload once.
        if (LoadToken()) out = HttpOnce(method, path, body, timeout_ms, status_out);
    }
    return out;
}

struct Resp {
    int kind;
    int status;
    std::string body;
};

static void PostHttp(int kind, std::wstring method, std::wstring path,
                     std::string body = "", int timeout_ms = 4000) {
    HWND target = g_hwnd;
    std::thread([kind, method, path, body, timeout_ms, target]() {
        int status = 0;
        std::string r = HttpCall(method, path, body, timeout_ms, &status);
        auto* resp = new Resp{kind, status, r};
        if (!PostMessageW(target, WM_APP_HTTP, (WPARAM)kind, (LPARAM)resp)) delete resp;
    }).detach();
}

// --------------------------------------------------------------------------- //
// engine
// --------------------------------------------------------------------------- //

static bool PortOpen(int port, int timeout_ms = 150) {
    SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == INVALID_SOCKET) return false;
    u_long nonblocking = 1;
    ioctlsocket(sock, FIONBIO, &nonblocking);
    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)port);
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");
    bool open = false;
    if (connect(sock, (sockaddr*)&addr, sizeof(addr)) == 0) {
        open = true;
    } else if (WSAGetLastError() == WSAEWOULDBLOCK) {
        fd_set writable;
        FD_ZERO(&writable);
        FD_SET(sock, &writable);
        timeval tv = { 0, timeout_ms * 1000 };
        if (select(0, nullptr, &writable, nullptr, &tv) > 0) {
            int err = 0;
            int len = sizeof(err);
            getsockopt(sock, SOL_SOCKET, SO_ERROR, (char*)&err, &len);
            open = (err == 0);
        }
    }
    closesocket(sock);
    return open;
}

// --------------------------------------------------------------------------- //
// quota: direct HTTPS, independent of the local relay
// --------------------------------------------------------------------------- //

static std::map<std::string, std::string> ReadEnvFile() {
    std::map<std::string, std::string> env;
    std::string raw = FileRead(g_exe_dir + L"\\.env");
    size_t pos = 0;
    while (pos <= raw.size()) {
        size_t end = raw.find('\n', pos);
        std::string line = Trim(raw.substr(pos, end == std::string::npos ? std::string::npos : end - pos));
        pos = (end == std::string::npos) ? raw.size() + 1 : end + 1;
        if (line.empty() || line[0] == '#') continue;
        size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        std::string key = Trim(line.substr(0, eq));
        std::string value = Trim(line.substr(eq + 1));
        if (value.size() >= 2 &&
            ((value.front() == '"' && value.back() == '"') ||
             (value.front() == '\'' && value.back() == '\''))) {
            value = value.substr(1, value.size() - 2);
        }
        if (!key.empty()) env[key] = value;
    }
    return env;
}

static std::string JsonObject(const std::string& json, const std::string& name) {
    size_t p = json.find("\"" + name + "\"");
    if (p == std::string::npos) return "";
    p = json.find('{', p);
    if (p == std::string::npos) return "";
    int depth = 0;
    bool in_string = false, escape = false;
    for (size_t i = p; i < json.size(); ++i) {
        char c = json[i];
        if (in_string) {
            if (escape) escape = false;
            else if (c == '\\') escape = true;
            else if (c == '"') in_string = false;
        } else {
            if (c == '"') in_string = true;
            else if (c == '{') ++depth;
            else if (c == '}') {
                if (--depth == 0) return json.substr(p, i - p + 1);
            }
        }
    }
    return "";
}

static bool JsonNumber(const std::string& obj, const std::string& key, double& out) {
    size_t p = obj.find("\"" + key + "\"");
    if (p == std::string::npos) return false;
    p = obj.find(':', p);
    if (p == std::string::npos) return false;
    const char* start = obj.c_str() + p + 1;
    char* end = nullptr;
    out = strtod(start, &end);
    return end != start;
}

static std::string JsonString(const std::string& obj, const std::string& key) {
    size_t p = obj.find("\"" + key + "\"");
    if (p == std::string::npos) return "";
    p = obj.find(':', p);
    if (p == std::string::npos) return "";
    p = obj.find('"', p);
    if (p == std::string::npos) return "";
    std::string out;
    for (size_t i = p + 1; i < obj.size(); ++i) {
        char c = obj[i];
        if (c == '\\' && i + 1 < obj.size()) { out += obj[++i]; continue; }
        if (c == '"') break;
        out += c;
    }
    return out;
}

static std::string FormatReset(const std::string& iso) {
    if (iso.size() < 16) return "-";
    int y = 0, mo = 0, d = 0, h = 0, mi = 0;
    if (sscanf(iso.c_str(), "%d-%d-%dT%d:%d", &y, &mo, &d, &h, &mi) != 5) return "-";
    struct tm tmv = {};
    tmv.tm_year = y - 1900;
    tmv.tm_mon = mo - 1;
    tmv.tm_mday = d;
    tmv.tm_hour = h;
    tmv.tm_min = mi;
    time_t epoch = _mkgmtime(&tmv);
    struct tm local = {};
    localtime_s(&local, &epoch);
    char buf[32] = {};
    strftime(buf, sizeof(buf), "%m-%d %H:%M", &local);
    return buf;
}

static bool FetchUsageOnce(const std::string& key, const std::string& proxy,
                           std::string& body, int& status, std::string& error) {
    body.clear();
    status = 0;
    error.clear();
    HINTERNET ses = WinHttpOpen(L"ocgo-panel/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY,
                                WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!ses) { error = "winhttp-open"; return false; }
    WinHttpSetTimeouts(ses, 1500, 2500, 3000, 10000);
    DWORD protos = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2;
    WinHttpSetOption(ses, WINHTTP_OPTION_SECURE_PROTOCOLS, &protos, sizeof(protos));

    std::wstring proxy_host;
    std::wstring proxy_user, proxy_pass;
    if (!proxy.empty()) {
        std::string rest = proxy;
        size_t scheme = rest.find("://");
        if (scheme != std::string::npos) rest = rest.substr(scheme + 3);
        std::string creds;
        size_t at = rest.rfind('@');
        if (at != std::string::npos) { creds = rest.substr(0, at); rest = rest.substr(at + 1); }
        if (!creds.empty()) {
            size_t colon = creds.find(':');
            proxy_user = U8ToW(creds.substr(0, colon));
            if (colon != std::string::npos) proxy_pass = U8ToW(creds.substr(colon + 1));
        }
        proxy_host = U8ToW(rest);
        WINHTTP_PROXY_INFO info = {};
        info.dwAccessType = WINHTTP_ACCESS_TYPE_NAMED_PROXY;
        info.lpszProxy = (LPWSTR)proxy_host.c_str();
        info.lpszProxyBypass = (LPWSTR)L"<local>";
        WinHttpSetOption(ses, WINHTTP_OPTION_PROXY, &info, sizeof(info));
    }

    HINTERNET con = WinHttpConnect(ses, L"opencode.ai", 443, 0);
    if (!con) { WinHttpCloseHandle(ses); error = "connect"; return false; }
    HINTERNET req = WinHttpOpenRequest(con, L"GET", L"/inference/go/v1/usage", nullptr,
                                       WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                       WINHTTP_FLAG_SECURE);
    if (!req) { WinHttpCloseHandle(con); WinHttpCloseHandle(ses); error = "request"; return false; }

    DWORD flags = WINHTTP_DISABLE_REDIRECTS;
    WinHttpSetOption(req, WINHTTP_OPTION_DISABLE_FEATURE, &flags, sizeof(flags));
    if (!proxy_user.empty()) {
        WinHttpSetOption(req, WINHTTP_OPTION_PROXY_USERNAME, (LPVOID)proxy_user.c_str(),
                         (DWORD)(proxy_user.size() * sizeof(wchar_t)));
        if (!proxy_pass.empty())
            WinHttpSetOption(req, WINHTTP_OPTION_PROXY_PASSWORD, (LPVOID)proxy_pass.c_str(),
                             (DWORD)(proxy_pass.size() * sizeof(wchar_t)));
    }

    std::wstring headers = L"Authorization: Bearer " + U8ToW(key) +
                           L"\r\nAccept: application/json\r\nUser-Agent: ocgo-panel/1.0\r\n";
    BOOL ok = WinHttpSendRequest(req, headers.c_str(), (DWORD)-1,
                                 WINHTTP_NO_REQUEST_DATA, 0, 0, 0);
    if (ok) ok = WinHttpReceiveResponse(req, nullptr);
    if (!ok) {
        error = "send";
        WinHttpCloseHandle(req); WinHttpCloseHandle(con); WinHttpCloseHandle(ses);
        return false;
    }

    DWORD code = 0, len = sizeof(code);
    WinHttpQueryHeaders(req, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                        WINHTTP_HEADER_NAME_BY_INDEX, &code, &len, WINHTTP_NO_HEADER_INDEX);
    status = (int)code;
    DWORD avail = 0;
    while (WinHttpQueryDataAvailable(req, &avail) && avail > 0) {
        std::string chunk(avail, '\0');
        DWORD read = 0;
        if (!WinHttpReadData(req, &chunk[0], avail, &read)) break;
        chunk.resize(read);
        body += chunk;
        if (body.size() > 1024 * 1024) break;
    }
    WinHttpCloseHandle(req);
    WinHttpCloseHandle(con);
    WinHttpCloseHandle(ses);
    return true;
}

static std::string QuotaRowsFromJson(const std::string& json) {
    struct Bucket { const char* name; const wchar_t* label; };
    static const Bucket buckets[3] = {
        { "rolling", L"5小时" }, { "weekly", L"本周" }, { "monthly", L"本月" }
    };
    std::string rows;
    for (const auto& b : buckets) {
        std::string obj = JsonObject(json, b.name);
        double percent = 0;
        bool has = JsonNumber(obj, "percent", percent);
        int pct = has ? (int)(percent + 0.5) : -1;
        if (pct > 100) pct = 100;
        std::string reset = FormatReset(JsonString(obj, "resetsAt"));
        std::string status = JsonString(obj, "status");
        rows += "R " + WToU8(b.label) + "|" + std::to_string(pct) + "|" + reset + "|" + status + "\n";
    }
    return rows;
}

static std::string BuildQuotaPayload(const std::string& key, const std::string& mode,
                                     const std::string& proxy) {
    std::string body, error;
    int status = 0;
    bool ok = FetchUsageOnce(key, "", body, status, error);
    if (ok && status == 200) {
        std::string rows = QuotaRowsFromJson(body);
        return rows.empty() ? "E nodata|无订阅数据" : rows;
    }
    if (status == 401) return "E badkey|Key 无效";
    if (status == 403) return "E nosub|无有效订阅或地区受限";

    if (mode.empty() || mode == "off" || proxy.empty())
        return "E net|网络不通（直连失败）";

    ok = FetchUsageOnce(key, proxy, body, status, error);
    if (ok && status == 200) {
        std::string rows = QuotaRowsFromJson(body);
        return rows.empty() ? "E nodata|无订阅数据" : rows;
    }
    if (status == 401) return "E badkey|Key 无效";
    if (status == 403) return "E nosub|无有效订阅或地区受限";
    return "E net|网络不通（代理 " + proxy + " 失败）";
}

static void PostQuotaDirect() {
    if (g_quota_busy) return;
    g_quota_busy = true;
    HWND target = g_hwnd;
    std::thread([target]() {
        auto env = ReadEnvFile();
        std::string key = env.count("OPENCODE_API_KEY") ? env["OPENCODE_API_KEY"] : "";
        std::string mode = env.count("OPENCODE_UPSTREAM_PROXY") ? env["OPENCODE_UPSTREAM_PROXY"] : "off";
        std::string proxy = env.count("OPENCODE_UPSTREAM_PROXY_URL") ? env["OPENCODE_UPSTREAM_PROXY_URL"] : "";
        if (mode != "off" && mode != "auto" && mode != "manual") {
            if (mode.find("://") != std::string::npos) proxy = mode;
            mode = "manual";
        }
        if ((mode == "auto" || mode == "manual") && proxy.empty())
            proxy = "http://127.0.0.1:7897";
        std::string payload = key.empty() ? "E nokey|未配置 API Key"
                                          : BuildQuotaPayload(key, mode, proxy);
        auto* resp = new Resp{K_QUOTA_DIRECT, 200, payload};
        if (!PostMessageW(target, WM_APP_HTTP, K_QUOTA_DIRECT, (LPARAM)resp)) delete resp;
    }).detach();
}

// --------------------------------------------------------------------------- //
// python runtime discovery
// --------------------------------------------------------------------------- //

enum LaunchState {
    LAUNCH_NONE = 0,
    LAUNCH_STARTING,
    LAUNCH_ALREADY,
    LAUNCH_PORT_BUSY,
    LAUNCH_NO_RUNTIME,
    LAUNCH_FAILED
};

static LaunchState g_launch_state = LAUNCH_NONE;
static bool g_started_by_panel = false;
static ULONGLONG g_launch_tick = 0;
static const ULONGLONG kLaunchGraceMs = 15000;

static bool FileExists(const std::wstring& path) {
    if (path.empty()) return false;
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

static std::wstring ExpandEnvPath(const std::wstring& pattern) {
    DWORD need = ExpandEnvironmentStringsW(pattern.c_str(), nullptr, 0);
    if (need == 0) return pattern;
    std::wstring out(need, L'\0');
    DWORD got = ExpandEnvironmentStringsW(pattern.c_str(), &out[0], need);
    if (got == 0 || got > need) return pattern;
    out.resize(got - 1);
    return out;
}

static std::wstring PathJoin(const std::wstring& dir, const std::wstring& name) {
    if (dir.empty()) return name;
    wchar_t last = dir[dir.size() - 1];
    if (last == L'\\' || last == L'/') return dir + name;
    return dir + L"\\" + name;
}

static std::wstring PythonIn(const std::wstring& dir) {
    std::wstring direct = PathJoin(dir, L"pythonw.exe");
    return FileExists(direct) ? direct : L"";
}

static std::wstring FindVersionedPython(const std::wstring& versions_dir) {
    std::wstring pattern = PathJoin(versions_dir, L"*");
    WIN32_FIND_DATAW fd = {};
    HANDLE h = FindFirstFileW(pattern.c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return L"";
    std::wstring found;
    FILETIME newest = {};
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0) continue;
        std::wstring candidate = PythonIn(PathJoin(versions_dir, fd.cFileName));
        if (candidate.empty()) continue;
        int order = CompareFileTime(&fd.ftLastWriteTime, &newest);
        if (found.empty() || order > 0 || (order == 0 && candidate > found)) {
            found = candidate;
            newest = fd.ftLastWriteTime;
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return found;
}

static std::wstring PythonFromWorkBuddyProcess(DWORD pid) {
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return L"";
    wchar_t image[MAX_PATH];
    DWORD length = ARRAYSIZE(image);
    BOOL queried = QueryFullProcessImageNameW(process, 0, image, &length);
    CloseHandle(process);
    if (!queried) return L"";
    std::wstring path(image, length);
    size_t parent = path.find_last_of(L"\\/");
    if (parent == std::wstring::npos) return L"";
    return PythonIn(PathJoin(path.substr(0, parent), L"resources\\vendor\\python"));
}

static std::wstring FindRunningWorkBuddyPython() {
    DWORD session = 0;
    if (!ProcessIdToSessionId(GetCurrentProcessId(), &session)) return L"";
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return L"";
    PROCESSENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    std::wstring found;
    for (BOOL more = Process32FirstW(snapshot, &entry); more;
         more = Process32NextW(snapshot, &entry)) {
        if (_wcsicmp(entry.szExeFile, L"WorkBuddy.exe") != 0) continue;
        DWORD other_session = 0;
        if (!ProcessIdToSessionId(entry.th32ProcessID, &other_session) || other_session != session)
            continue;
        found = PythonFromWorkBuddyProcess(entry.th32ProcessID);
        if (!found.empty()) break;
    }
    CloseHandle(snapshot);
    return found;
}

static std::wstring FindPythonw() {
    std::wstring found = FindVersionedPython(ExpandEnvPath(
        L"%USERPROFILE%\\.workbuddy\\binaries\\python\\versions"));
    if (!found.empty()) return found;
    found = FindRunningWorkBuddyPython();
    if (!found.empty()) return found;

    const wchar_t* install_dirs[] = {
        L"%ProgramFiles%\\WorkBuddy\\resources\\vendor\\python",
        L"%LOCALAPPDATA%\\Programs\\WorkBuddy\\resources\\vendor\\python",
    };
    for (const wchar_t* dir : install_dirs) {
        found = PythonIn(ExpandEnvPath(dir));
        if (!found.empty()) return found;
    }
    std::wstring search = ExpandEnvPath(L"%PATH%");
    wchar_t resolved[MAX_PATH];
    if (!search.empty() && search != L"%PATH%") {
        DWORD length = SearchPathW(search.c_str(), L"pythonw.exe", nullptr,
                                   ARRAYSIZE(resolved), resolved, nullptr);
        if (length > 0 && length < ARRAYSIZE(resolved) && FileExists(resolved))
            return std::wstring(resolved, length);
    }
    return L"";
}

// Keeps the launch outcomes distinguishable: started here, already running,
// port 8787 busy, runtime missing, or CreateProcess failure.
static void StartEngine() {
    if (g_running) {
        g_launch_state = LAUNCH_ALREADY;
        SetText(IDC_STATUS, L"● 转发桥已在运行 · 端口 8787");
        SetHintUser(L"转发桥已经在运行，无需重复启动");
        RefreshStatusAsync();
        return;
    }
    if (g_launch_state == LAUNCH_STARTING &&
        GetTickCount64() - g_launch_tick < kLaunchGraceMs) {
        SetText(IDC_STATUS, L"… 正在启动转发桥 · 端口 8787 ...");
        SetHintUser(L"转发桥正在启动，请稍候 ...");
        return;
    }

    g_launch_state = LAUNCH_NONE;
    g_started_by_panel = false;
    if (PortOpen(8787, 250)) {
        int status = 0;
        std::string body = HttpCall(L"GET", L"/api/status", "", 800, &status);
        if (status == 200 && body.rfind("S ", 0) == 0) {
            g_launch_state = LAUNCH_ALREADY;
            SetText(IDC_STATUS, L"● 转发桥已在运行 · 端口 8787");
            SetHintUser(L"转发桥已经在运行，无需重复启动");
            RefreshStatusAsync();
            return;
        }
        g_launch_state = LAUNCH_PORT_BUSY;
        SetText(IDC_STATUS, L"✕ 端口 8787 已被其他程序占用");
        SetHintUser(L"端口 8787 被占用，转发桥未启动");
        MessageBoxW(g_hwnd,
                    L"端口 8787 已被其他程序占用，转发桥未启动。\n\n"
                    L"请关闭占用该端口的程序后重试。",
                    L"OCSub2WB", MB_ICONWARNING);
        return;
    }

    std::wstring py = FindPythonw();
    if (py.empty()) {
        g_launch_state = LAUNCH_NO_RUNTIME;
        SetText(IDC_STATUS, L"✕ 未找到 Python 运行时");
        SetHintUser(L"缺少 pythonw.exe：请检查 WorkBuddy 用户目录或安装目录");
        MessageBoxW(g_hwnd,
                    L"未找到 pythonw.exe。请启动 WorkBuddy 后重试。\n\n"
                    L"如果 WorkBuddy 已经打开，请检查它自带的 Python。",
                    L"OCSub2WB", MB_ICONERROR);
        return;
    }

    std::wstring script = g_exe_dir + L"\\opencode-go-proxy.py";
    if (!FileExists(script) || !FileExists(PathJoin(g_exe_dir, L"ocgo_common.py"))) {
        g_launch_state = LAUNCH_FAILED;
        SetText(IDC_STATUS, L"✕ 缺少转发桥脚本");
        SetHintUser(L"请把压缩包里的两个 Python 文件放回 EXE 同一目录");
        MessageBoxW(g_hwnd, L"请重新解压 ZIP。两个 Python 文件需要和 EXE 放在同一目录。",
                    L"OCSub2WB", MB_ICONERROR);
        return;
    }

    std::wstring cmd = L"\"" + py + L"\" \"" + script + L"\"";
    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi = {};
    std::vector<wchar_t> mutable_cmd(cmd.begin(), cmd.end());
    mutable_cmd.push_back(L'\0');
    if (CreateProcessW(py.c_str(), mutable_cmd.data(), nullptr, nullptr, FALSE,
                       CREATE_NO_WINDOW, nullptr, g_exe_dir.c_str(), &si, &pi)) {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        g_launch_state = LAUNCH_STARTING;
        g_started_by_panel = true;
        g_launch_tick = GetTickCount64();
        SetText(IDC_STATUS, L"… 正在启动转发桥 · 端口 8787 ...");
        SetHintUser(L"已启动 pythonw.exe，正在等待端口 8787 就绪 ...");
    } else {
        DWORD err = GetLastError();
        g_launch_state = LAUNCH_FAILED;
        SetText(IDC_STATUS, L"✕ 转发桥启动失败");
        SetHintUser(L"无法启动 pythonw.exe（错误 " + std::to_wstring(err) + L"）");
        MessageBoxW(g_hwnd,
                    (L"无法启动 pythonw.exe：\n" + py + L"\n\n系统错误码 " +
                     std::to_wstring(err)).c_str(),
                    L"OCSub2WB", MB_ICONERROR);
    }
}

// --------------------------------------------------------------------------- //
// UI helpers
// --------------------------------------------------------------------------- //

enum ResizeAnchor : unsigned {
    MoveRight = 1 << 0,
    GrowWidth = 1 << 1,
    MoveBelowModels = 1 << 2,
    GrowModelHeight = 1 << 3,
    GrowLogHeight = 1 << 4
};

struct ResizableControl {
    HWND hwnd;
    int x, y, width, height;
    unsigned anchors;
};

static std::vector<ResizableControl> g_resizable_controls;

static HWND Mk(const wchar_t* cls, const wchar_t* text, DWORD style,
               int x, int y, int w, int h, int id, unsigned anchors = 0, DWORD ex = 0) {
    HWND child = CreateWindowExW(ex, cls, text, WS_CHILD | WS_VISIBLE | style,
                                 x, y, w, h, g_hwnd, (HMENU)(INT_PTR)id, nullptr, nullptr);
    if (child && g_font) SendMessageW(child, WM_SETFONT, (WPARAM)g_font, TRUE);
    if (child && anchors) g_resizable_controls.push_back({child, x, y, w, h, anchors});
    return child;
}

static void ResizeControls(int width, int height) {
    const int extra_width = std::max(0, width - CLIENT_W);
    const int extra_height = std::max(0, height - CLIENT_H);
    const int extra_models = extra_height / 2;
    const int extra_log = extra_height - extra_models;

    HDWP batch = BeginDeferWindowPos(static_cast<int>(g_resizable_controls.size()));
    for (const auto& control : g_resizable_controls) {
        if (!batch) return;
        const unsigned anchors = control.anchors;
        const int x = control.x + ((anchors & MoveRight) ? extra_width : 0);
        const int y = control.y + ((anchors & MoveBelowModels) ? extra_models : 0);
        const int w = control.width + ((anchors & GrowWidth) ? extra_width : 0);
        const int h = control.height + ((anchors & GrowModelHeight) ? extra_models : 0)
                                      + ((anchors & GrowLogHeight) ? extra_log : 0);
        batch = DeferWindowPos(batch, control.hwnd, nullptr, x, y, w, h,
                               SWP_NOZORDER | SWP_NOACTIVATE);
    }
    if (batch) EndDeferWindowPos(batch);
}

static void SetText(int id, const std::wstring& text) {
    SetWindowTextW(GetDlgItem(g_hwnd, id), text.c_str());
}

static void SetHintUser(const std::wstring& text) {
    g_hint_sticky = false;
    SetText(IDC_HINT, text);
}

static void SetHintSticky(const std::wstring& text) {
    g_hint_sticky = true;
    SetText(IDC_HINT, text);
}

static void SetHintAuto(const std::wstring& text) {
    if (!g_hint_sticky) SetText(IDC_HINT, text);
}

static bool ProxyCharAllowed(wchar_t c) {
    if (c < 32) return true;
    if (c >= '0' && c <= '9') return true;
    return c == '.' || c == ':' || c == '/' ||
           c == 'h' || c == 't' || c == 'p' ||
           c == 'H' || c == 'T' || c == 'P';
}

static LRESULT CALLBACK ProxyEditSubclass(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp,
                                          UINT_PTR, DWORD_PTR) {
    if (msg == WM_CHAR && !ProxyCharAllowed((wchar_t)wp)) return 0;
    if (msg == WM_PASTE) {
        if (!OpenClipboard(hwnd)) return 0;
        std::wstring text;
        HANDLE data = GetClipboardData(CF_UNICODETEXT);
        if (data) {
            void* raw = GlobalLock(data);
            if (raw) {
                text = (const wchar_t*)raw;
                GlobalUnlock(data);
            }
        }
        CloseClipboard();
        std::wstring filtered;
        for (wchar_t c : text) if (ProxyCharAllowed(c)) filtered += c;
        if (!filtered.empty())
            SendMessageW(hwnd, EM_REPLACESEL, TRUE, (LPARAM)filtered.c_str());
        return 0;
    }
    return DefSubclassProc(hwnd, msg, wp, lp);
}

static constexpr const wchar_t* RECENT_REQUEST_COLUMNS[] = {L"模型 ID", L"tok/s", L"TTFT (s)"};
using RecentRequestRow = std::array<std::wstring, 3>;
static std::vector<RecentRequestRow> g_recent_requests;
static void ResizeRecentRequestColumns();

static void CreateControls() {
    Mk(L"STATIC", L"", SS_LEFT | SS_CENTERIMAGE, 12, 10, 600, 22, IDC_STATUS, GrowWidth);

    Mk(L"BUTTON", L"启动转发桥", BS_PUSHBUTTON, 620, 8, 120, 28, IDC_BTN_START, MoveRight);
    Mk(L"BUTTON", L"打开日志目录", BS_PUSHBUTTON, 748, 8, 120, 28, IDC_BTN_DIR, MoveRight);
    Mk(L"BUTTON", L"刷新用量", BS_PUSHBUTTON, 876, 8, 80, 28, IDC_Q_REFRESH, MoveRight);
    Mk(L"BUTTON", L"退出", BS_PUSHBUTTON, 964, 8, 92, 28, IDC_BTN_EXIT, MoveRight);

    Mk(L"BUTTON", L"额度", BS_GROUPBOX, 12, 42, 1056, 126, -1, GrowWidth);
    const wchar_t* qlabels[3] = { L"5 小时", L"本周", L"本月" };
    for (int i = 0; i < 3; ++i) {
        int y = 66 + i * 30;
        Mk(L"STATIC", qlabels[i], SS_LEFT | SS_CENTERIMAGE, 26, y, 62, 16, IDC_Q_LABEL1 + i);
        Mk(PROGRESS_CLASSW, L"", PBS_SMOOTH, 92, y, 250, 16, IDC_Q_BAR1 + i);
        SendMessageW(GetDlgItem(g_hwnd, IDC_Q_BAR1 + i), PBM_SETRANGE32, 0, 100);
        Mk(L"STATIC", L"-", SS_LEFT | SS_CENTERIMAGE | SS_ENDELLIPSIS,
           354, y, 230, 16, IDC_Q_INFO1 + i);
    }
    Mk(L"STATIC", L"最近完成的请求", SS_LEFT | SS_CENTERIMAGE, 600, 43, 250, 17, -1);
    HWND recent = Mk(WC_LISTVIEWW, L"", LVS_REPORT | LVS_SINGLESEL | LVS_NOSORTHEADER,
                     600, 61, 456, 104, IDC_RECENT_REQUESTS, GrowWidth, WS_EX_CLIENTEDGE);
    ListView_SetExtendedListViewStyle(recent, LVS_EX_FULLROWSELECT | LVS_EX_GRIDLINES | LVS_EX_DOUBLEBUFFER);
    LVCOLUMNW recent_column = {};
    recent_column.mask = LVCF_TEXT | LVCF_FMT;
    recent_column.fmt = LVCFMT_LEFT;
    for (int i = 0; i < 3; ++i) {
        recent_column.pszText = const_cast<wchar_t*>(RECENT_REQUEST_COLUMNS[i]);
        ListView_InsertColumn(recent, i, &recent_column);
    }
    HWND recent_header = ListView_GetHeader(recent);
    SetWindowLongPtrW(recent_header, GWL_STYLE,
                      GetWindowLongPtrW(recent_header, GWL_STYLE) | HDS_NOSIZING);
    ResizeRecentRequestColumns();

    Mk(L"BUTTON", L"模型", BS_GROUPBOX, 12, 176, 1056, 250, IDC_MODEL_GROUP, GrowWidth | GrowModelHeight);
    HWND lv = Mk(WC_LISTVIEWW, L"", LVS_REPORT | LVS_SINGLESEL | LVS_SHOWSELALWAYS | WS_BORDER,
                 24, 200, 1032, 176, IDC_MODELS, GrowWidth | GrowModelHeight, WS_EX_CLIENTEDGE);
    ListView_SetExtendedListViewStyle(lv, LVS_EX_FULLROWSELECT | LVS_EX_GRIDLINES | LVS_EX_DOUBLEBUFFER);
    SetWindowSubclass(lv, ModelsSubclass, 1, 0);
    struct { const wchar_t* name; int width; } cols[] = {
        { L"模型 id", 176 }, { L"名称", 200 }, { L"上下文", 88 }, { L"输出", 88 },
        { L"思考", 64 }, { L"档位", 110 }, { L"规则", 142 }, { L"探测", 140 }
    };
    for (int i = 0; i < 8; ++i) {
        LVCOLUMNW col = {};
        col.mask = LVCF_TEXT | LVCF_WIDTH | LVCF_SUBITEM;
        col.pszText = (LPWSTR)cols[i].name;
        col.cx = cols[i].width;
        col.iSubItem = i;
        ListView_InsertColumn(lv, i, &col);
    }

    Mk(L"BUTTON", L"一键配置", BS_PUSHBUTTON, 24, 384, 84, 28, IDC_BTN_REFRESH, MoveBelowModels);
    Mk(L"BUTTON", L"探测免费模型", BS_PUSHBUTTON, 116, 384, 104, 28, IDC_BTN_PROBE, MoveBelowModels);
    Mk(L"BUTTON", L"导入列表至WB", BS_PUSHBUTTON, 228, 384, 112, 28, IDC_BTN_IMPORT, MoveBelowModels);
    Mk(L"BUTTON", L"测试已选模型", BS_PUSHBUTTON, 348, 384, 104, 28, IDC_BTN_SELFTEST, MoveBelowModels);
    Mk(L"BUTTON", L"刷新模型状态", BS_PUSHBUTTON, 460, 384, 96, 28, IDC_BTN_REFRESH_LIST, MoveBelowModels);
    Mk(L"BUTTON", L"清空WB列表", BS_PUSHBUTTON, 564, 384, 96, 28, IDC_BTN_PURGE, MoveBelowModels);
    Mk(L"STATIC", L"", SS_LEFT | SS_CENTERIMAGE, 668, 386, 400, 22, IDC_HINT, GrowWidth | MoveBelowModels);
    SetRelayOnly(false);

    Mk(L"STATIC", L"API Key", SS_LEFT | SS_CENTERIMAGE, 12, 439, 62, 20, -1, MoveBelowModels);
    HWND key = Mk(L"EDIT", L"", ES_AUTOHSCROLL | WS_BORDER, 78, 436, 280, 24, IDC_KEY, MoveBelowModels, WS_EX_CLIENTEDGE);
    SendMessageW(key, EM_SETCUEBANNER, TRUE, (LPARAM)L"粘贴新的 oc_sk_... 后点保存");
    Mk(L"BUTTON", L"保存", BS_PUSHBUTTON, 364, 436, 64, 24, IDC_BTN_KEY, MoveBelowModels);

    Mk(L"BUTTON", L"直连", BS_AUTORADIOBUTTON | WS_GROUP, 444, 438, 60, 20, IDC_RADIO_OFF, MoveBelowModels);
    Mk(L"BUTTON", L"自动", BS_AUTORADIOBUTTON, 508, 438, 60, 20, IDC_RADIO_AUTO, MoveBelowModels);
    Mk(L"BUTTON", L"手动", BS_AUTORADIOBUTTON, 572, 438, 60, 20, IDC_RADIO_MANUAL, MoveBelowModels);
    HWND url = Mk(L"EDIT", L"http://127.0.0.1:7897", ES_AUTOHSCROLL | WS_BORDER,
                  636, 436, 240, 24, IDC_PROXY_URL, MoveBelowModels, WS_EX_CLIENTEDGE);
    SendMessageW(url, EM_SETCUEBANNER, TRUE, (LPARAM)L"http://127.0.0.1:7897");
    SendMessageW(url, EM_SETLIMITTEXT, 128, 0);
    SetWindowSubclass(url, ProxyEditSubclass, 2, 0);
    Mk(L"BUTTON", L"应用", BS_PUSHBUTTON, 884, 436, 64, 24, IDC_BTN_PROXY, MoveBelowModels);

    Mk(L"BUTTON", L"日志", BS_GROUPBOX, 12, 470, 1056, 266, -1, GrowWidth | MoveBelowModels | GrowLogHeight);
    HWND log = Mk(L"EDIT", L"", ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL | WS_VSCROLL | WS_BORDER,
                  24, 494, 1032, 230, IDC_LOG, GrowWidth | MoveBelowModels | GrowLogHeight, WS_EX_CLIENTEDGE);
    SendMessageW(log, EM_SETLIMITTEXT, 200000, 0);
    SendMessageW(log, WM_SETFONT, (WPARAM)g_font, TRUE);

    CheckRadioButton(g_hwnd, IDC_RADIO_OFF, IDC_RADIO_MANUAL, IDC_RADIO_OFF);
    EnableWindow(GetDlgItem(g_hwnd, IDC_PROXY_URL), FALSE);
}

// --------------------------------------------------------------------------- //
// model list
// --------------------------------------------------------------------------- //

static std::wstring FmtTokens(long long n) {
    if (n < 0) return L"—";
    if (n >= 1000000) return std::to_wstring(n / 1000000) + L"M";
    if (n >= 1000) return std::to_wstring(n / 1000) + L"K";
    return std::to_wstring(n);
}

static std::string RulesText(const ModelRow& m) {
    std::string text = m.free ? "免费" : "付费";
    if (m.training) text += "·训练";
    if (m.limited) text += "·锁区";
    if (m.zdr) text += "·ZDR";
    return text;
}


static int TextCmp(const std::string& a, const std::string& b) {
    static const LCID kZh = MAKELCID(MAKELANGID(LANG_CHINESE, SUBLANG_CHINESE_SIMPLIFIED),
                                     SORT_DEFAULT);
    std::wstring wa = U8ToW(a), wb = U8ToW(b);
    int r = CompareStringW(kZh, NORM_IGNORECASE, wa.c_str(), -1, wb.c_str(), -1);
    if (r == CSTR_LESS_THAN) return -1;
    if (r == CSTR_GREATER_THAN) return 1;
    return 0;
}

static int EffortCount(const std::string& efforts) {
    int count = 0;
    size_t start = 0;
    while (start <= efforts.size()) {
        size_t end = efforts.find(',', start);
        std::string part = efforts.substr(start, end == std::string::npos
                                                     ? std::string::npos : end - start);
        if (!part.empty()) ++count;
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return count;
}

static bool SortLess(const ModelRow& a, const ModelRow& b) {
    if (g_sort_col == 7 && a.probe.empty() != b.probe.empty()) {
        return !a.probe.empty();
    }
    if (g_sort_col == 2 || g_sort_col == 3) {
        bool an = (g_sort_col == 2 ? a.ctx : a.out) < 0;
        bool bn = (g_sort_col == 2 ? b.ctx : b.out) < 0;
        if (an != bn) return !an;  // unknown always last
    }
    int cmp = 0;
    switch (g_sort_col) {
        case 0: cmp = TextCmp(a.id, b.id); break;
        case 1: cmp = TextCmp(a.name, b.name); break;
        case 2: cmp = a.ctx < b.ctx ? -1 : (a.ctx > b.ctx ? 1 : 0); break;
        case 3: cmp = a.out < b.out ? -1 : (a.out > b.out ? 1 : 0); break;
        case 4: cmp = TextCmp(WToU8(ThinkLabel(a.think)), WToU8(ThinkLabel(b.think))); break;
        case 5: {
            int ca = EffortCount(a.efforts), cb = EffortCount(b.efforts);
            cmp = ca < cb ? -1 : (ca > cb ? 1 : 0);
            break;
        }
        case 6: cmp = TextCmp(RulesText(a), RulesText(b)); break;
        case 7: cmp = TextCmp(a.probe, b.probe); break;
        default: break;
    }
    if (cmp != 0) return g_sort_asc ? cmp < 0 : cmp > 0;
    return a.id < b.id;
}

struct SelPos { int row = -1; int col = -1; int ch = 0; };

static SelPos g_sel_anchor, g_sel_focus;
static bool g_sel_active = false;
static bool g_sel_dragging = false;
static POINT g_sel_origin = {0, 0};

static HWND ModelsList() { return GetDlgItem(g_hwnd, IDC_MODELS); }

static int SelCmp(const SelPos& a, const SelPos& b) {
    if (a.row != b.row) return a.row < b.row ? -1 : 1;
    if (a.col != b.col) return a.col < b.col ? -1 : 1;
    if (a.ch != b.ch) return a.ch < b.ch ? -1 : 1;
    return 0;
}

static std::wstring CellTextAt(int row, int col) {
    wchar_t buf[512] = {};
    LVITEMW item = {};
    item.iSubItem = col;
    item.pszText = buf;
    item.cchTextMax = 511;
    SendMessageW(ModelsList(), LVM_GETITEMTEXTW, (WPARAM)row, (LPARAM)&item);
    return buf;
}

static bool CellTextRect(int row, int col, RECT& rect, int& text_left) {
    HWND lv = ModelsList();
    if (col == 0) {
        RECT rc = { LVIR_LABEL, 0, 0, 0 };
        if (!SendMessageW(lv, LVM_GETITEMRECT, (WPARAM)row, (LPARAM)&rc)) return false;
        rect = rc;
        text_left = rc.left;
    } else {
        RECT rc = {};
        rc.left = LVIR_BOUNDS;
        rc.top = col;
        if (!SendMessageW(lv, LVM_GETSUBITEMRECT, (WPARAM)row, (LPARAM)&rc)) return false;
        rect = rc;
        text_left = rc.left + 6;
    }
    return true;
}

static void MeasurePrefixes(HDC dc, const std::wstring& text, std::vector<int>& dx) {
    int len = (int)text.size();
    dx.assign(len, 0);
    if (len == 0) return;
    int fit = 0;
    SIZE size = {};
    if (!GetTextExtentExPointW(dc, text.c_str(), len, INT_MAX, &fit, dx.data(), &size)) {
        for (int i = 0; i < len; ++i) {
            SIZE part = {};
            GetTextExtentPoint32W(dc, text.c_str(), i + 1, &part);
            dx[i] = part.cx;
        }
    }
}

static int CharFromX(int row, int col, int x) {
    RECT rect;
    int left = 0;
    if (!CellTextRect(row, col, rect, left)) return 0;
    std::wstring text = CellTextAt(row, col);
    int len = (int)text.size();
    if (len == 0) return 0;
    int rel = x - left;
    if (rel <= 0) return 0;
    HDC dc = GetDC(ModelsList());
    HGDIOBJ old = SelectObject(dc, g_font);
    std::vector<int> dx;
    MeasurePrefixes(dc, text, dx);
    SelectObject(dc, old);
    ReleaseDC(ModelsList(), dc);
    for (int i = 0; i < len; ++i) if (dx[i] > rel) return i;
    return len;
}

static SelPos HitSelPos(POINT pt) {
    LVHITTESTINFO hti = {};
    hti.pt = pt;
    SendMessageW(ModelsList(), LVM_SUBITEMHITTEST, 0, (LPARAM)&hti);
    if (hti.iItem < 0) return SelPos{-1, -1, 0};
    int col = hti.iSubItem < 0 ? 0 : hti.iSubItem;
    return SelPos{ hti.iItem, col, CharFromX(hti.iItem, col, pt.x) };
}

static void ClearTextSelection(bool repaint) {
    bool had = g_sel_active;
    g_sel_active = false;
    g_sel_dragging = false;
    g_sel_anchor = SelPos{};
    g_sel_focus = SelPos{};
    if (repaint && had) InvalidateRect(ModelsList(), nullptr, FALSE);
}

static void BuildSelectionText(std::wstring& out) {
    out.clear();
    if (!g_sel_active) return;
    SelPos a = g_sel_anchor, b = g_sel_focus;
    if (SelCmp(a, b) > 0) std::swap(a, b);
    for (int row = a.row; row <= b.row; ++row) {
        int first_col = (row == a.row) ? a.col : 0;
        int last_col = (row == b.row) ? b.col : 7;
        bool first_cell = true;
        for (int col = first_col; col <= last_col; ++col) {
            std::wstring text = CellTextAt(row, col);
            int begin = (row == a.row && col == a.col) ? a.ch : 0;
            int end = (row == b.row && col == b.col) ? b.ch : (int)text.size();
            if (begin < 0) begin = 0;
            if (end > (int)text.size()) end = (int)text.size();
            if (end < begin) end = begin;
            if (!first_cell) out += L'\t';
            out.append(text, begin, end - begin);
            first_cell = false;
        }
        if (row < b.row) out += L"\r\n";
    }
}

static void CopySelection() {
    if (!g_sel_active) return;
    std::wstring text;
    BuildSelectionText(text);
    if (text.empty()) return;
    if (!OpenClipboard(ModelsList())) return;
    EmptyClipboard();
    size_t bytes = (text.size() + 1) * sizeof(wchar_t);
    HGLOBAL mem = GlobalAlloc(GMEM_MOVEABLE, bytes);
    if (mem) {
        void* dst = GlobalLock(mem);
        if (dst) {
            memcpy(dst, text.c_str(), bytes);
            GlobalUnlock(mem);
            if (SetClipboardData(CF_UNICODETEXT, mem)) mem = nullptr;
        }
        if (mem) GlobalFree(mem);
    }
    CloseClipboard();
}

static void BlendRect(HDC dc, HDC mem, const RECT& rect) {
    if (rect.right <= rect.left || rect.bottom <= rect.top) return;
    BLENDFUNCTION bf = { AC_SRC_OVER, 0, 90, 0 };
    GdiAlphaBlend(dc, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top,
                  mem, 0, 0, 1, 1, bf);
}

static void DrawTextSelection(HDC dc) {
    if (!g_sel_active) return;
    SelPos a = g_sel_anchor, b = g_sel_focus;
    if (SelCmp(a, b) > 0) std::swap(a, b);
    HDC mem = CreateCompatibleDC(dc);
    HBITMAP bmp = CreateCompatibleBitmap(dc, 1, 1);
    HGDIOBJ old_bmp = SelectObject(mem, bmp);
    RECT one = {0, 0, 1, 1};
    HBRUSH brush = CreateSolidBrush(RGB(150, 195, 255));
    FillRect(mem, &one, brush);
    DeleteObject(brush);

    HGDIOBJ old_font = SelectObject(dc, g_font);
    for (int row = a.row; row <= b.row; ++row) {
        int first_col = (row == a.row) ? a.col : 0;
        int last_col = (row == b.row) ? b.col : 7;
        for (int col = first_col; col <= last_col; ++col) {
            std::wstring text = CellTextAt(row, col);
            if (text.empty()) continue;
            int begin = (row == a.row && col == a.col) ? a.ch : 0;
            int end = (row == b.row && col == b.col) ? b.ch : (int)text.size();
            if (begin < 0) begin = 0;
            if (end > (int)text.size()) end = (int)text.size();
            if (end <= begin) continue;
            RECT rect;
            int left = 0;
            if (!CellTextRect(row, col, rect, left)) continue;
            int len = (int)text.size();
            std::vector<int> dx;
            MeasurePrefixes(dc, text, dx);
            int x1 = left + (begin == 0 ? 0 : dx[begin - 1]);
            int x2 = left + (end == 0 ? 0 : dx[end - 1]);
            RECT hl = { x1, rect.top + 2, x2, rect.bottom - 2 };
            if (hl.left < rect.left) hl.left = rect.left;
            if (hl.right > rect.right) hl.right = rect.right;
            BlendRect(dc, mem, hl);
        }
    }
    SelectObject(dc, old_font);
    SelectObject(mem, old_bmp);
    DeleteObject(bmp);
    DeleteDC(mem);
}

static LRESULT CALLBACK ModelsSubclass(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp,
                                       UINT_PTR, DWORD_PTR) {
    switch (msg) {
        case WM_LBUTTONDOWN: {
            SetFocus(hwnd);
            POINT pt = { GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
            g_sel_origin = pt;
            g_sel_dragging = false;
            SelPos pos = HitSelPos(pt);
            if (pos.row < 0) {
                ClearTextSelection(true);
                break;
            }
            ListView_SetItemState(hwnd, -1, 0, LVIS_SELECTED | LVIS_FOCUSED);
            g_sel_anchor = pos;
            g_sel_focus = pos;
            g_sel_active = false;
            SetCapture(hwnd);
            return 0;
        }
        case WM_MOUSEMOVE: {
            if (GetCapture() != hwnd) break;
            POINT pt = { GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
            if (!g_sel_active) {
                int dx = abs(pt.x - g_sel_origin.x);
                int dy = abs(pt.y - g_sel_origin.y);
                if (dx < GetSystemMetrics(SM_CXDRAG) && dy < GetSystemMetrics(SM_CYDRAG))
                    break;
                g_sel_active = true;
                g_sel_dragging = true;
            }
            SelPos pos = HitSelPos(pt);
            if (pos.row < 0) {
                int count = ListView_GetItemCount(hwnd);
                if (count > 0) {
                    pos.row = pt.y < 0 ? 0 : count - 1;
                    pos.col = 0;
                    pos.ch = 0;
                }
            }
            if (pos.row >= 0) g_sel_focus = pos;
            InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        }
        case WM_LBUTTONUP: {
            if (GetCapture() == hwnd) ReleaseCapture();
            g_sel_dragging = false;
            if (!g_sel_active) {
                if (g_sel_anchor.row >= 0) {
                    ListView_SetItemState(hwnd, g_sel_anchor.row,
                                          LVIS_SELECTED | LVIS_FOCUSED,
                                          LVIS_SELECTED | LVIS_FOCUSED);
                }
                ClearTextSelection(true);
            }
            return 0;
        }
        case WM_KEYDOWN:
            if ((GetKeyState(VK_CONTROL) & 0x8000) && (wp == 'C' || wp == 'c')) {
                CopySelection();
                return 0;
            }
            break;
        case WM_CHAR:
            if (wp == 3) { CopySelection(); return 0; }
            break;
        default:
            break;
    }
    return DefSubclassProc(hwnd, msg, wp, lp);
}

static void UpdateSortHeader() {
    static const wchar_t* kBase[8] = {
        L"模型 id", L"名称", L"上下文", L"输出", L"思考", L"档位", L"规则", L"探测"
    };
    HWND lv = GetDlgItem(g_hwnd, IDC_MODELS);
    if (!lv) return;
    HWND hdr = ListView_GetHeader(lv);
    if (!hdr) return;
    for (int i = 0; i < 8; ++i) {
        HDITEMW hi = {};
        hi.mask = HDI_FORMAT;
        if (!Header_GetItem(hdr, i, &hi)) continue;
        hi.fmt &= ~(HDF_SORTUP | HDF_SORTDOWN);
        if (i == g_sort_col) hi.fmt |= g_sort_asc ? HDF_SORTUP : HDF_SORTDOWN;
        std::wstring text = kBase[i];
        if (i == g_sort_col) text += g_sort_asc ? L" ▲" : L" ▼";
        hi.mask = HDI_FORMAT | HDI_TEXT;
        hi.pszText = (LPWSTR)text.c_str();
        hi.cchTextMax = (int)text.size();
        Header_SetItem(hdr, i, &hi);
    }
}

static int MeasureTextWidth(HDC dc, HFONT font, const std::wstring& text) {
    if (text.empty()) return 0;
    HGDIOBJ old = SelectObject(dc, font);
    SIZE size = {};
    GetTextExtentPoint32W(dc, text.c_str(), (int)text.size(), &size);
    SelectObject(dc, old);
    return size.cx;
}

static void ResizeRecentRequestColumns() {
    HWND list = GetDlgItem(g_hwnd, IDC_RECENT_REQUESTS);
    RECT client = {};
    if (!GetClientRect(list, &client) || client.right < 5) return;
    HDC dc = GetDC(list);
    if (!dc) return;

    HWND header = ListView_GetHeader(list);
    HFONT cell_font = (HFONT)SendMessageW(list, WM_GETFONT, 0, 0);
    HFONT header_font = (HFONT)SendMessageW(header, WM_GETFONT, 0, 0);
    if (!cell_font) cell_font = g_font;
    if (!header_font) header_font = cell_font;

    std::array<int, 3> needs = {};
    for (int i = 0; i < 3; ++i)
        needs[i] = MeasureTextWidth(dc, header_font, RECENT_REQUEST_COLUMNS[i]);
    needs[1] = std::max(needs[1], MeasureTextWidth(dc, cell_font, L"9999.9"));
    for (const auto& row : g_recent_requests) {
        for (int i = 0; i < 3; ++i)
            needs[i] = std::max(needs[i], MeasureTextWidth(dc, cell_font, row[i]));
    }
    const int padding = MulDiv(16, std::max(96, GetDeviceCaps(dc, LOGPIXELSX)), 96);
    ReleaseDC(list, dc);

    const int available = static_cast<int>(client.right) - 2;
    const int spare = std::max(0, available - needs[0] - needs[1] - needs[2] - 3 * padding);
    const int rate_width = std::min(available - 2, needs[1] + padding + spare / 3);
    const int ttft_width = std::min(available - rate_width - 1, needs[2] + padding + spare / 3);
    ListView_SetColumnWidth(list, 0, available - rate_width - ttft_width);
    ListView_SetColumnWidth(list, 1, rate_width);
    ListView_SetColumnWidth(list, 2, ttft_width);
}

static void AutoSizeColumns() {
    HWND lv = GetDlgItem(g_hwnd, IDC_MODELS);
    if (!lv) return;
    HWND hdr = ListView_GetHeader(lv);
    if (!hdr) return;

    static const wchar_t* kBase[8] = {
        L"模型 id", L"名称", L"上下文", L"输出", L"思考", L"档位", L"规则", L"探测"
    };
    HDC dc = GetDC(lv);
    if (!dc) return;
    HFONT cell_font = (HFONT)SendMessageW(lv, WM_GETFONT, 0, 0);
    HFONT head_font = (HFONT)SendMessageW(hdr, WM_GETFONT, 0, 0);
    if (!cell_font) cell_font = g_font;
    if (!head_font) head_font = g_font;

    int dpi = GetDeviceCaps(dc, LOGPIXELSY);
    if (dpi <= 0) dpi = 96;
    int pad = MulDiv(14, dpi, 96);
    int id_floor = MulDiv(56, dpi, 96);

    int desired[8] = {};
    int header_w[8] = {};
    for (int i = 0; i < 8; ++i) {
        std::wstring up = std::wstring(kBase[i]) + L" ▲";
        std::wstring down = std::wstring(kBase[i]) + L" ▼";
        header_w[i] = std::max(MeasureTextWidth(dc, head_font, up),
                               MeasureTextWidth(dc, head_font, down));
        desired[i] = header_w[i];
    }

    for (const auto& m : g_models) {
        int widths[8] = {
            MeasureTextWidth(dc, cell_font, U8ToW(m.id)),
            MeasureTextWidth(dc, cell_font, U8ToW(m.name)),
            MeasureTextWidth(dc, cell_font, FmtTokens(m.ctx)),
            MeasureTextWidth(dc, cell_font, FmtTokens(m.out)),
            MeasureTextWidth(dc, cell_font, ThinkLabel(m.think)),
            MeasureTextWidth(dc, cell_font, EffortsLabel(m)),
            MeasureTextWidth(dc, cell_font, U8ToW(RulesText(m))),
            MeasureTextWidth(dc, cell_font, U8ToW(m.probe))
        };
        for (int i = 0; i < 8; ++i) desired[i] = std::max(desired[i], widths[i]);
    }
    for (int i = 0; i < 8; ++i) desired[i] += pad;

    int id_min = std::max(header_w[0], id_floor) + pad;
    RECT rc = {};
    GetClientRect(lv, &rc);
    int avail = std::max(0, (int)rc.right - 2);

    int sum_all = 0;
    for (int i = 0; i < 8; ++i) sum_all += desired[i];

    int widths[8] = {};
    int sum = 0;
    if (sum_all <= avail) {
        for (int i = 0; i < 8; ++i) widths[i] = desired[i];
        sum = sum_all;
    } else {
        widths[0] = id_min;
        sum = id_min;
        for (int i = 1; i < 8; ++i) {
            widths[i] = desired[i];
            sum += widths[i];
        }
    }

    if (sum < avail) {
        int extra = avail - sum;
        int each = extra / 8;
        int rem = extra % 8;
        for (int i = 0; i < 8; ++i) {
            widths[i] += each + (i < rem ? 1 : 0);
        }
    }

    SendMessageW(lv, WM_SETREDRAW, FALSE, 0);
    for (int i = 0; i < 8; ++i) {
        if (widths[i] < 1) widths[i] = 1;
        ListView_SetColumnWidth(lv, i, widths[i]);
    }
    SendMessageW(lv, WM_SETREDRAW, TRUE, 0);
    RedrawWindow(lv, nullptr, nullptr, RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_FRAME);
    ReleaseDC(lv, dc);
}

static void FillModels() {
    ClearTextSelection(false);
    HWND lv = GetDlgItem(g_hwnd, IDC_MODELS);
    int selected = ListView_GetNextItem(lv, -1, LVNI_SELECTED);
    std::string keep_id;
    if (selected >= 0 && selected < (int)g_models.size()) keep_id = g_models[selected].id;
    if (g_sort_col >= 0) std::stable_sort(g_models.begin(), g_models.end(), SortLess);
    ListView_DeleteAllItems(lv);
    for (size_t i = 0; i < g_models.size(); ++i) {
        const ModelRow& m = g_models[i];
        std::wstring id = U8ToW(m.id);
        LVITEMW item = {};
        item.mask = LVIF_TEXT | LVIF_PARAM;
        item.iItem = (int)i;
        item.pszText = (LPWSTR)id.c_str();
        item.lParam = (LPARAM)i;
        ListView_InsertItem(lv, &item);

        std::wstring name = U8ToW(m.name);
        std::wstring ctx = FmtTokens(m.ctx);
        std::wstring out = FmtTokens(m.out);
        std::wstring think = ThinkLabel(m.think);
        std::wstring eff = EffortsLabel(m);
        std::wstring src = U8ToW(RulesText(m));
        std::wstring probe = U8ToW(m.probe);
        ListView_SetItemText(lv, (int)i, 1, (LPWSTR)name.c_str());
        ListView_SetItemText(lv, (int)i, 2, (LPWSTR)ctx.c_str());
        ListView_SetItemText(lv, (int)i, 3, (LPWSTR)out.c_str());
        ListView_SetItemText(lv, (int)i, 4, (LPWSTR)think.c_str());
        ListView_SetItemText(lv, (int)i, 5, (LPWSTR)eff.c_str());
        ListView_SetItemText(lv, (int)i, 6, (LPWSTR)src.c_str());
        ListView_SetItemText(lv, (int)i, 7, (LPWSTR)probe.c_str());
    }
    if (!keep_id.empty()) {
        for (size_t i = 0; i < g_models.size(); ++i) {
            if (g_models[i].id != keep_id) continue;
            ListView_SetItemState(lv, (int)i, LVIS_SELECTED | LVIS_FOCUSED,
                                  LVIS_SELECTED | LVIS_FOCUSED);
            ListView_EnsureVisible(lv, (int)i, FALSE);
            break;
        }
    }
    UpdateSortHeader();
    AutoSizeColumns();
}

static std::string ProbeReason(const std::string& kind) {
    if (kind == "401 key") return "Key无效";
    if (kind == "403 global" || kind == "400 global") return "需开Global";
    if (kind == "403 region" || kind == "400 region" || kind == "451 region")
        return "地区受限";
    if (kind == "403 plan") return "无订阅";
    if (kind == "403 free") return "免费档限制";
    if (kind == "opt-in paid") return "需开付费训练";
    if (kind == "opt-in free") return "需开免费训练";
    if (kind == "opt-in") return "未开训练";
    if (kind == "429 rate") return "限流";
    if (kind == "waf") return "被WAF拦截";
    if (kind == "400 session" || kind == "400") return "请求错误";
    if (kind == "需要代理") return "锁区需代理";
    if (kind.rfind("error", 0) == 0 || kind == "no proxy") return "网络失败";
    return "未知错误";
}

static std::string ProbeSummary(const ProbeRow& row) {
    if (row.route == "proxy") {
        if (row.status == 200) return "代理可用";
        return "不可用·" + ProbeReason(row.kind);
    }
    if (row.status == 200) return "可直连";
    return "不可用·" + ProbeReason(row.kind);
}

static void ApplyProbeToModels() {
    for (auto& row : g_probe) {
        for (auto& m : g_models) {
            if (m.id != row.id) continue;
            std::string summary = ProbeSummary(row);
            if (m.training && (summary == "可直连" || summary == "代理可用"))
                summary += "·需训练";
            m.probe = summary;
        }
    }
    // Probed (free) models go to the top, otherwise they hide below 27 paid rows.
    std::stable_sort(g_models.begin(), g_models.end(), [](const ModelRow& a, const ModelRow& b) {
        return !a.probe.empty() && b.probe.empty();
    });
    FillModels();
}

// --------------------------------------------------------------------------- //
// actions
// --------------------------------------------------------------------------- //

static void RefreshStatusAsync() {
    if (g_status_busy) return;
    g_status_busy = true;
    PostHttp(K_STATUS, L"GET", L"/api/status", "", 3000);
}

static void RefreshQuotaAsync() {
    // Quota belongs to the API key, not to the local relay: fetch it directly.
    PostQuotaDirect();
}

static void RefreshModelsAsync() {
    PostHttp(K_MODELS, L"GET", L"/api/models", "", 30000);
}

static void RefreshLogAsync() {
    PostHttp(K_LOG, L"GET", L"/api/log?lines=250", "", 4000);
}

static void DoRefreshModels() {
    if (!g_running) return;
    int imported = 0, free = 0;
    for (const auto& m : g_models) {
        if (m.imported) ++imported;
        if (m.free) ++free;
    }
    std::wstring ask = L"将删除现有 " + std::to_wstring(imported) +
                       L" 条 OC 托管模型，并按实时目录重建（含 " + std::to_wstring(free) +
                       L" 个免费模型）。\n自有模型会保留。\n\n继续？";
    if (MessageBoxW(g_hwnd, ask.c_str(), L"一键配置",
                    MB_YESNO | MB_ICONQUESTION) != IDYES) {
        return;
    }
    SetHintSticky(L"正在一键配置 ...");
    g_action_sticky = true;
    PostHttp(K_ACTION, L"POST", L"/api/refresh?reset=1", "free=all", 90000);
}

static void DoStartStop() {
    g_action_sticky = false;
    if (g_running) {
        g_action_sticky = false;
        PostHttp(K_ACTION, L"POST", L"/api/shutdown", "", 5000);
        SetHintUser(L"正在停止转发桥 ...");
    } else {
        StartEngine();
    }
}

static void DoExit() {
    if (MessageBoxW(g_hwnd, L"退出将同时停止转发桥，进行中的对话会中断。\n\n继续退出？",
                    L"OCSub2WB", MB_YESNO | MB_ICONWARNING) != IDYES) {
        return;
    }
    EnableWindow(GetDlgItem(g_hwnd, IDC_BTN_EXIT), FALSE);
    SetText(IDC_STATUS, L"正在停止转发桥 ...");
    HWND target = g_hwnd;
    std::thread([target]() {
        int status = 0;
        HttpCall(L"POST", L"/api/shutdown", "", 2500, &status);
        // The HTTP reply comes back before the relay has actually exited, so
        // wait for the port to close instead of closing the window immediately.
        for (int i = 0; i < 20 && PortOpen(8787); ++i) Sleep(150);
        PostMessageW(target, WM_APP_EXIT_NOW, 0, 0);
    }).detach();
}

static void DoProbe() {
    if (!g_running) return;
    if (g_job_id.empty()) g_probe_busy = false;
    if (g_probe_busy) return;
    g_job_kind = 0;
    g_job_model.clear();
    g_probe_busy = true;
    SetHintSticky(L"开始探测 ...");
    PostHttp(K_PROBE_START, L"POST", L"/api/probe", "", 10000);
}

static void DoSelftest() {
    if (!g_running) return;
    if (g_probe_busy) {
        SetHintSticky(L"已有任务在运行");
        return;
    }
    if (g_models.empty()) {
        MessageBoxW(g_hwnd, L"模型列表还没加载完。", L"测试已选模型", MB_ICONINFORMATION);
        return;
    }
    HWND lv = GetDlgItem(g_hwnd, IDC_MODELS);
    int index = ListView_GetNextItem(lv, -1, LVNI_SELECTED);
    if (index < 0 || index >= (int)g_models.size()) index = 0;  // 未选中则测第一行
    std::string model_id = g_models[index].id;
    std::wstring ask = L"只测试这一个模型：\n" + U8ToW(model_id) +
                       L"\n\n会发最多 2 次很小的请求，消耗少量额度；不影响正在进行的对话。继续？";
    if (MessageBoxW(g_hwnd, ask.c_str(), L"测试已选模型", MB_YESNO | MB_ICONQUESTION) != IDYES) {
        return;
    }
    g_probe_busy = true;
    g_job_kind = 1;
    g_job_model = model_id;
    SetHintSticky(L"测试中 ...");
    PostHttp(K_PROBE_START, L"POST", L"/api/selftest", "model=" + model_id, 10000);
}

static bool g_purge_fallback = false;

static void HandleLocalPurge(const std::string& body) {
    if (body.rfind("P ", 0) == 0) {
        auto kv = ParseKV(body);
        std::string msg = "已清空 " + kv["purged"] + " 条，保留 " + kv["kept"] + " 条自有模型";
        if (!g_running) {
            msg += "（转发桥未运行，列表暂不刷新）";
            g_models.clear();
            HWND lv = GetDlgItem(g_hwnd, IDC_MODELS);
            if (lv) ListView_DeleteAllItems(lv);
            UpdateModelGroupTitle(false);
        } else {
            RefreshModelsAsync();
        }
        SetHintSticky(U8ToW(msg));
        RefreshStatusAsync();
    } else {
        std::wstring msg = body.rfind("E ", 0) == 0 ? U8ToW(body.substr(2)) : L"本地清空失败";
        SetHintSticky(msg);
        MessageBoxW(g_hwnd, msg.c_str(), L"清空WB列表", MB_ICONWARNING);
    }
}

static void StartLocalPurge() {
    g_action_sticky = false;
    SetHintSticky(L"正在本地清空 ...");
    HWND target = g_hwnd;
    std::thread([target]() {
        std::wstring py = FindPythonw();
        std::wstring script = g_exe_dir + L"\\opencode-go-proxy.py";
        std::wstring result = g_exe_dir + L"\\cache\\purge.result." +
                              std::to_wstring(GetCurrentProcessId());
        DeleteFileW(result.c_str());
        std::string body;
        if (GetFileAttributesW(py.c_str()) == INVALID_FILE_ATTRIBUTES ||
            GetFileAttributesW(script.c_str()) == INVALID_FILE_ATTRIBUTES) {
            body = "E 找不到 pythonw 或 opencode-go-proxy.py";
        } else {
            std::wstring cmd = L"\"" + py + L"\" \"" + script +
                               L"\" --purge-models --result-file \"" + result + L"\"";
            STARTUPINFOW si = { sizeof(si) };
            PROCESS_INFORMATION pi = {};
            std::vector<wchar_t> mutable_cmd(cmd.begin(), cmd.end());
            mutable_cmd.push_back(L'\0');
            if (!CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr, FALSE,
                                CREATE_NO_WINDOW, nullptr, g_exe_dir.c_str(), &si, &pi)) {
                body = "E 无法启动本地清空进程";
            } else {
                DWORD waited = WaitForSingleObject(pi.hProcess, 15000);
                if (waited == WAIT_TIMEOUT) {
                    TerminateProcess(pi.hProcess, 1);
                    body = "E 本地清空超时";
                }
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
            }
            if (body.empty()) {
                body = FileRead(result);
                if (body.empty()) body = "E 子进程没有返回结果";
            }
            DeleteFileW(result.c_str());
        }
        auto* body_ptr = new std::string(body);
        if (!PostMessageW(target, WM_APP_JOB_DONE, 0, (LPARAM)body_ptr)) delete body_ptr;
    }).detach();
}

static void DoPurge() {
    int imported = 0;
    for (const auto& m : g_models) if (m.imported) ++imported;
    std::wstring ask;
    if (g_running) {
        if (imported > 0) {
            ask = L"将删除现有 " + std::to_wstring(imported) +
                  L" 条 OC 托管模型；自有模型保留，不会重建。\n\n继续？";
        } else {
            ask = L"将删除全部 OC 托管条目（当前列表未加载）；自有模型保留，不会重建。\n\n继续？";
        }
    } else {
        ask = L"转发桥未运行，将直接清理 models.json 中全部 OC 托管条目"
              L"（写前自动备份），自有模型保留。\n\n继续？";
    }
    if (MessageBoxW(g_hwnd, ask.c_str(), L"清空WB列表", MB_YESNO | MB_ICONWARNING) != IDYES) {
        return;
    }
    if (g_running) {
        g_purge_fallback = true;
        SetHintSticky(L"正在清空 ...");
        g_action_sticky = true;
        PostHttp(K_ACTION, L"POST", L"/api/workbuddy/purge", "", 30000);
    } else {
        StartLocalPurge();
    }
}

static void DoImportProbe() {
    if (!g_running) return;
    std::string ids;
    for (const auto& r : g_probe) {
        if (r.score() > 0) {
            if (!ids.empty()) ids += ",";
            ids += r.id;
        }
    }
    if (ids.empty()) {
        MessageBoxW(g_hwnd, L"还没有可用的探测结果。先点“探测免费模型”。", L"OCSub2WB", MB_ICONINFORMATION);
        return;
    }
    SetHintSticky(U8ToW("导入 " + ids));
    g_action_sticky = true;
    PostHttp(K_ACTION, L"POST", L"/api/refresh?reset=1", "free=" + ids, 90000);
}

static void DoSaveKey() {
    wchar_t buf[512] = {};
    GetWindowTextW(GetDlgItem(g_hwnd, IDC_KEY), buf, 512);
    std::string key = Trim(WToU8(buf));
    if (key.empty()) {
        MessageBoxW(g_hwnd, L"请先粘贴新的 oc_sk_ 密钥。", L"OCSub2WB", MB_ICONINFORMATION);
        return;
    }
    if (key.find("…") != std::string::npos) {
        MessageBoxW(g_hwnd, L"这是掩码显示，不是完整密钥。", L"OCSub2WB", MB_ICONINFORMATION);
        return;
    }
    SetHintUser(L"正在校验密钥 ...");
    g_action_sticky = false;
    PostHttp(K_ACTION, L"POST", L"/api/key", "key=" + key, 40000);
}


// --------------------------------------------------------------------------- //
// upstream proxy: preset / validate / friendly errors
// --------------------------------------------------------------------------- //

static bool RawReadFile(const std::wstring& path, std::string& data) {
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size = {};
    if (!GetFileSizeEx(h, &size)) { CloseHandle(h); return false; }
    data.resize((size_t)size.QuadPart);
    DWORD read = 0;
    BOOL ok = ReadFile(h, data.data(), (DWORD)data.size(), &read, nullptr);
    CloseHandle(h);
    data.resize(ok ? read : 0);
    return ok != FALSE;
}

static bool AtomicWriteFile(const std::wstring& path, const std::string& data) {
    std::wstring tmp = path + L".panel.tmp";
    HANDLE h = CreateFileW(tmp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    BOOL ok = WriteFile(h, data.data(), (DWORD)data.size(), &written, nullptr);
    if (ok) FlushFileBuffers(h);
    CloseHandle(h);
    if (!ok || written != data.size()) { DeleteFileW(tmp.c_str()); return false; }
    if (!MoveFileExW(tmp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING)) {
        DeleteFileW(tmp.c_str());
        return false;
    }
    return true;
}

static bool WriteEnvValues(const std::vector<std::pair<std::string, std::string>>& values) {
    for (const auto& kv : values) {
        if (kv.second.find_first_of("\r\n") != std::string::npos) return false;
    }
    std::wstring path = g_exe_dir + L"\\.env";
    std::string raw;
    if (!RawReadFile(path, raw)) return false;
    bool bom = raw.size() >= 3 && (unsigned char)raw[0] == 0xEF &&
               (unsigned char)raw[1] == 0xBB && (unsigned char)raw[2] == 0xBF;
    size_t start = bom ? 3 : 0;
    bool crlf = raw.find("\r\n") != std::string::npos;
    std::vector<std::string> lines;
    size_t pos = start;
    while (pos <= raw.size()) {
        size_t nl = raw.find('\n', pos);
        std::string line = raw.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        lines.push_back(line);
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
    if (!lines.empty() && lines.back().empty()) lines.pop_back();
    for (const auto& kv : values) {
        bool found = false;
        for (auto& line : lines) {
            if (line.rfind(kv.first + "=", 0) == 0) {
                line = kv.first + "=" + kv.second;
                found = true;
                break;
            }
        }
        if (!found) lines.push_back(kv.first + "=" + kv.second);
    }
    std::string out;
    if (bom) out.assign("\xEF\xBB\xBF", 3);
    for (size_t i = 0; i < lines.size(); ++i) {
        out += lines[i];
        if (i + 1 < lines.size()) out += crlf ? "\r\n" : "\n";
    }
    out += crlf ? "\r\n" : "\n";
    return AtomicWriteFile(path, out);
}

static bool WriteEnvValue(const std::string& key, const std::string& value) {
    return WriteEnvValues({ { key, value } });
}

static bool ParseProxyUrl(std::string url, std::string& host, int& port) {
    url = Trim(url);
    if (url.rfind("http://", 0) != 0) return false;
    std::string rest = url.substr(7);
    if (rest.empty() || rest.find('/') != std::string::npos ||
        rest.find('@') != std::string::npos) return false;
    size_t colon = rest.rfind(':');
    if (colon == std::string::npos) return false;
    host = rest.substr(0, colon);
    std::string port_text = rest.substr(colon + 1);
    if (host.empty() || port_text.empty()) return false;
    int parts = 0;
    size_t pos = 0;
    while (pos <= host.size()) {
        size_t dot = host.find('.', pos);
        std::string part = host.substr(pos, dot == std::string::npos
                                                 ? std::string::npos : dot - pos);
        if (part.empty() || part.size() > 3) return false;
        for (char c : part) if (c < '0' || c > '9') return false;
        int value = atoi(part.c_str());
        if (value < 0 || value > 255) return false;
        ++parts;
        if (dot == std::string::npos) break;
        pos = dot + 1;
    }
    if (parts != 4) return false;
    for (char c : port_text) if (c < '0' || c > '9') return false;
    port = atoi(port_text.c_str());
    return port > 0 && port < 65536;
}

static bool TcpProbe(const std::string& host, int port, int timeout_ms) {
    addrinfo hints = {};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* found = nullptr;
    std::string service = std::to_string(port);
    if (getaddrinfo(host.c_str(), service.c_str(), &hints, &found) != 0) return false;
    bool ok = false;
    for (addrinfo* p = found; p && !ok; p = p->ai_next) {
        SOCKET s = socket(p->ai_family, p->ai_socktype, p->ai_protocol);
        if (s == INVALID_SOCKET) continue;
        u_long nonblocking = 1;
        ioctlsocket(s, FIONBIO, &nonblocking);
        int rc = connect(s, p->ai_addr, (int)p->ai_addrlen);
        if (rc == 0) {
            ok = true;
        } else if (WSAGetLastError() == WSAEWOULDBLOCK) {
            fd_set writable;
            FD_ZERO(&writable);
            FD_SET(s, &writable);
            timeval tv = { timeout_ms / 1000, (timeout_ms % 1000) * 1000 };
            if (select(0, nullptr, &writable, nullptr, &tv) > 0) {
                int err = 0;
                int len = sizeof(err);
                getsockopt(s, SOL_SOCKET, SO_ERROR, (char*)&err, &len);
                ok = (err == 0);
            }
        }
        closesocket(s);
    }
    freeaddrinfo(found);
    return ok;
}

static std::wstring FriendlyError(const std::string& body) {
    std::string text = body;
    if (text.rfind("E ", 0) == 0) text = text.substr(2);
    std::string low = text;
    for (auto& c : low) c = (char)tolower((unsigned char)c);
    if (low == "send" || low == "receive" || low == "connect" ||
        low.rfind("winhttp", 0) == 0) {
        return L"无法连接转发桥，请先启动转发桥";
    }
    return U8ToW(text);
}

static std::string ProxyUrlText() {
    wchar_t buf[512] = {};
    GetWindowTextW(GetDlgItem(g_hwnd, IDC_PROXY_URL), buf, 512);
    return Trim(WToU8(buf));
}

static void ShowUrlError(const std::wstring& message) {
    g_proxy_url_invalid = true;
    HWND edit = GetDlgItem(g_hwnd, IDC_PROXY_URL);
    if (edit) {
        InvalidateRect(edit, nullptr, TRUE);
        EDITBALLOONTIP tip = {};
        tip.cbStruct = sizeof(tip);
        tip.pszTitle = L"代理地址不合法";
        tip.pszText = message.c_str();
        tip.ttiIcon = TTI_ERROR;
        SendMessageW(edit, EM_SHOWBALLOONTIP, 0, (LPARAM)&tip);
    }
    SetHintUser(message);
}

static void ClearUrlError() {
    if (!g_proxy_url_invalid) return;
    g_proxy_url_invalid = false;
    HWND edit = GetDlgItem(g_hwnd, IDC_PROXY_URL);
    if (edit) InvalidateRect(edit, nullptr, TRUE);
}

static void HandleProxyCheck(ProxyCheckResult* result) {
    if (result->ok) {
        if (WriteEnvValues({ { "OPENCODE_UPSTREAM_PROXY", result->mode },
                             { "OPENCODE_UPSTREAM_PROXY_URL", result->url } })) {
            g_proxy_dirty = false;
            g_proxy_applied = result->mode;
            g_proxy_mode = U8ToW(result->mode);
            g_proxy_url_synced = U8ToW(result->url);
            g_proxy_url_current = U8ToW(result->url);
            UpdateTrayTip();
            SetHintUser(result->mode == "auto"
                                  ? L"已保存；自动模式下仅锁区模型走此代理"
                                  : L"已保存；启动转发桥后生效");
        } else {
            MessageBoxW(g_hwnd, L"保存 .env 失败，请检查文件权限", L"上游代理",
                        MB_ICONWARNING);
        }
    } else {
        ShowUrlError(U8ToW("代理不可用：" + result->detail + " 连接失败"));
    }
    delete result;
}

static void ApplyProxyMode(const std::string& mode, const std::string& url) {
    bool bridge = g_running || PortOpen(8787);
    if (mode == "off") {
        if (bridge) {
            SetHintUser(L"正在切换上游代理 ...");
            g_action_sticky = false;
            PostHttp(K_ACTION, L"POST", L"/api/upstream-proxy",
                     "mode=off&url=" + url, 20000);
        } else if (WriteEnvValues({ { "OPENCODE_UPSTREAM_PROXY", "off" },
                                     { "OPENCODE_UPSTREAM_PROXY_URL", url } })) {
            g_proxy_dirty = false;
            g_proxy_applied = "off";
            g_proxy_mode = L"off";
            if (!url.empty()) g_proxy_url_current = U8ToW(url);
            UpdateTrayTip();
            SetHintUser(L"已保存，启动转发桥后生效");
        } else {
            MessageBoxW(g_hwnd, L"保存 .env 失败，请检查文件权限", L"上游代理",
                        MB_ICONWARNING);
        }
        return;
    }

    std::string host;
    int port = 0;
    if (!ParseProxyUrl(url, host, port)) {
        ShowUrlError(L"地址不合法，应为 http://IPv4:端口，例如 http://127.0.0.1:7897");
        return;
    }
    if (bridge) {
        g_proxy_pending = true;
        SetHintUser(L"正在切换上游代理 ...");
        g_action_sticky = false;
        PostHttp(K_ACTION, L"POST", L"/api/upstream-proxy",
                 "mode=" + mode + "&url=" + url, 20000);
        return;
    }
    SetHintUser(L"正在验证代理 ...");
    std::thread([mode, url, host, port]() {
        bool ok = TcpProbe(host, port, 2000);
        auto* result = new ProxyCheckResult{ mode, url, ok, host + ":" + std::to_string(port) };
        if (!PostMessageW(g_hwnd, WM_APP_PROXY_CHECK, 0, (LPARAM)result)) delete result;
    }).detach();
}

static void OnProxyRadio(int id) {
    if (id == IDC_RADIO_MANUAL) {
        g_proxy_dirty = true;
        EnableWindow(GetDlgItem(g_hwnd, IDC_PROXY_URL), TRUE);
        SetFocus(GetDlgItem(g_hwnd, IDC_PROXY_URL));
        SetHintUser(L"修改地址后点「应用」");
        return;
    }
    if (id == IDC_RADIO_OFF) {
        EnableWindow(GetDlgItem(g_hwnd, IDC_PROXY_URL), FALSE);
        if (!g_proxy_dirty && g_proxy_applied == "off") return;
        g_proxy_dirty = false;
        ApplyProxyMode("off", ProxyUrlText());
        return;
    }
    EnableWindow(GetDlgItem(g_hwnd, IDC_PROXY_URL), TRUE);
    if (!g_proxy_dirty && g_proxy_applied == "auto") return;
    g_proxy_dirty = true;
    SetHintUser(L"自动：仅锁区模型走此代理");
    ApplyProxyMode("auto", ProxyUrlText());
}

static void DoApplyProxy() {
    std::string mode = "off";
    if (Button_GetCheck(GetDlgItem(g_hwnd, IDC_RADIO_AUTO)) == BST_CHECKED) {
        mode = "auto";
    } else if (Button_GetCheck(GetDlgItem(g_hwnd, IDC_RADIO_MANUAL)) == BST_CHECKED) {
        mode = "manual";
    }
    std::string url = ProxyUrlText();
    if (mode != "off" && url.empty()) {
        ShowUrlError(L"请填写代理地址，例如 http://127.0.0.1:7897");
        return;
    }
    if (mode != "off") g_proxy_dirty = true;
    ApplyProxyMode(mode, url);
}

static void SetRecentRequests(const std::vector<RecentRequestRow>& rows) {
    if (rows == g_recent_requests) return;
    g_recent_requests = rows;

    HWND list = GetDlgItem(g_hwnd, IDC_RECENT_REQUESTS);
    SendMessageW(list, WM_SETREDRAW, FALSE, 0);
    ListView_DeleteAllItems(list);
    for (int i = 0; i < static_cast<int>(rows.size()); ++i) {
        const auto& [model, rate, ttft] = rows[i];
        LVITEMW item = {};
        item.mask = LVIF_TEXT;
        item.iItem = i;
        item.pszText = const_cast<wchar_t*>(model.c_str());
        int inserted = ListView_InsertItem(list, &item);
        if (inserted >= 0) {
            ListView_SetItemText(list, inserted, 1, const_cast<wchar_t*>(rate.c_str()));
            ListView_SetItemText(list, inserted, 2, const_cast<wchar_t*>(ttft.c_str()));
        }
    }
    ResizeRecentRequestColumns();
    SendMessageW(list, WM_SETREDRAW, TRUE, 0);
    InvalidateRect(list, nullptr, TRUE);
}

static void HandleStatus(const Resp* r) {
    g_status_busy = false;
    if (r->status != 200 || r->body.rfind("S ", 0) != 0) {
        SetRecentRequests({{L"等待转发桥", L"—", L"—"}});
        g_running = false;
        // Keep "started by this panel" across the first polls while the
        // engine is still booting; a later failure clears it.
        if (g_launch_state != LAUNCH_STARTING)
            g_started_by_panel = false;
        if (g_launch_state == LAUNCH_STARTING &&
            GetTickCount64() - g_launch_tick < kLaunchGraceMs) {
            SetText(IDC_STATUS, L"… 正在启动转发桥 · 端口 8787 ...");
            SetText(IDC_BTN_START, L"启动转发桥");
            SetRelayOnly(false);
            UpdateTrayTip();
            return;
        }
        if (g_launch_state == LAUNCH_STARTING) {
            g_launch_state = LAUNCH_FAILED;
            SetHintUser(L"转发桥启动超时：端口 8787 未就绪，请查看日志");
        } else if (g_launch_state == LAUNCH_ALREADY) {
            g_launch_state = LAUNCH_NONE;
        }
        switch (g_launch_state) {
            case LAUNCH_NO_RUNTIME:
                SetText(IDC_STATUS, L"✕ 未找到 Python 运行时");
                break;
            case LAUNCH_PORT_BUSY:
                SetText(IDC_STATUS, L"✕ 端口 8787 已被其他程序占用");
                break;
            case LAUNCH_FAILED:
                SetText(IDC_STATUS, L"✕ 转发桥启动失败 · 端口 8787 未就绪");
                break;
            default:
                SetText(IDC_STATUS, L"○ 转发桥未运行");
                break;
        }
        SetText(IDC_BTN_START, L"启动转发桥");
        SetRelayOnly(false);
        UpdateTrayTip();
        return;
    }
    auto kv = ParseKV(r->body);
    bool was_running = g_running;
    bool just_started = (g_launch_state == LAUNCH_STARTING);
    bool already_running = (g_launch_state == LAUNCH_ALREADY);
    g_launch_state = LAUNCH_NONE;
    g_running = true;
    g_key_masked = kv["key"];
    std::vector<RecentRequestRow> recent;
    const bool supports_timing = kv.find("latest_ttft_s") != kv.end();
    if (supports_timing) {
        for (const std::string prefix : {"latest", "recent_2", "recent_3"}) {
            const auto model = U8ToW(kv[prefix + "_model"]);
            if (model.empty()) break;
            const auto rate = U8ToW(kv[prefix + "_tok_s"]);
            const auto ttft = U8ToW(kv[prefix + "_ttft_s"]);
            recent.push_back({model, rate.empty() ? L"—" : rate, ttft.empty() ? L"—" : ttft});
        }
    }
    if (recent.empty()) {
        recent.push_back({supports_timing ? L"等待模型请求" : L"请重启至新版转发桥", L"—", L"—"});
    }
    SetRecentRequests(recent);
    SetRelayOnly(true);

    // First successful status after the relay came up: pull the data that
    // failed while it was still starting.
    if (!was_running) {
        RefreshModelsAsync();
        RefreshLogAsync();
        if (just_started)
            SetHintAuto(L"转发桥已启动");
        else if (already_running)
            SetHintAuto(L"转发桥已经在运行");
        else
            SetHintAuto(L"已连接");
    }

    // A restarted relay forgets its jobs; drop any stale poll immediately.
    if (!g_engine_pid.empty() && g_engine_pid != kv["pid"]) {
        g_job_id.clear();
        g_probe_busy = false;
        KillTimer(g_hwnd, 3);
    }
    g_engine_pid = kv["pid"];

    std::wstring mode = U8ToW(kv["mode"]);
    std::wstring proxy_url = U8ToW(kv["proxy_url"]);
    if (mode.find(L"://") != std::wstring::npos) {
        proxy_url = mode;
        mode = L"manual";
    }
    g_proxy_mode = mode;
    g_proxy_url_current = proxy_url;
    g_status_port = U8ToW(kv["port"]);
    std::wstring models = U8ToW(kv["models"]);
    std::wstring pid = U8ToW(kv["pid"]);

    std::wstring route;
    if (mode == L"off") route = L"直连";
    else if (mode == L"auto") route = L"自动（锁区→" + proxy_url + L"）";
    else route = L"手动→" + proxy_url;

    std::wstring models_state = U8ToW(kv["models_state"]);
    std::wstring headline = g_started_by_panel ? L"● 转发桥已启动"
                                               : L"● 转发桥已在运行";
    std::wstring text = headline + L" · 端口 " + U8ToW(kv["port"]) +
                        L" · 上游 " + route +
                        L" · " + models + L" 个模型 · pid " + pid;
    if (models_state == L"recovered") text += L" · models.json 已回退备份";
    if (g_proxy_dirty) text += L" · 地址未应用";
    SetText(IDC_STATUS, text);
    SetText(IDC_BTN_START, L"停止转发桥");

    if (models_state == L"recovered" && !g_recovery_prompted) {
        g_recovery_prompted = true;
        ShowPanel();
        if (MessageBoxW(g_hwnd,
                        L"models.json 损坏，已从备份只读加载。\n\n是否写回最新备份并恢复？",
                        L"OCSub2WB", MB_YESNO | MB_ICONWARNING) == IDYES) {
            g_restore_pending = true;
            g_action_sticky = false;
            SetHintUser(L"正在恢复 models.json ...");
            PostHttp(K_ACTION, L"POST", L"/api/models/restore", "", 20000);
        } else {
            SetHintUser(L"models.json 已回退备份，写操作锁定；重启可再确认");
        }
    }

    if (!g_proxy_dirty) {
        g_proxy_syncing = true;
        if (mode == L"off") {
            CheckRadioButton(g_hwnd, IDC_RADIO_OFF, IDC_RADIO_MANUAL, IDC_RADIO_OFF);
            g_proxy_applied = "off";
        } else if (mode == L"auto") {
            CheckRadioButton(g_hwnd, IDC_RADIO_OFF, IDC_RADIO_MANUAL, IDC_RADIO_AUTO);
            g_proxy_applied = "auto";
        } else {
            CheckRadioButton(g_hwnd, IDC_RADIO_OFF, IDC_RADIO_MANUAL, IDC_RADIO_MANUAL);
            g_proxy_applied = "manual";
        }
        if (!proxy_url.empty()) {
            g_proxy_url_synced = proxy_url;
            SetWindowTextW(GetDlgItem(g_hwnd, IDC_PROXY_URL), proxy_url.c_str());
        }
        EnableWindow(GetDlgItem(g_hwnd, IDC_PROXY_URL), mode != L"off");
        g_proxy_syncing = false;
    }
    UpdateTrayTip();
}

static void RenderQuota(const std::string& body) {
    int index = 0;
    bool any_valid = false;
    for (const auto& line : Lines(body)) {
        if (line.rfind("R ", 0) != 0) continue;
        auto parts = Split(line.substr(2), '|');
        if (parts.size() < 4 || index >= 3) continue;
        int percent = atoi(parts[1].c_str());
        int pos = percent < 0 ? 0 : (percent > 100 ? 100 : percent);
        SendMessageW(GetDlgItem(g_hwnd, IDC_Q_BAR1 + index), PBM_SETPOS, (WPARAM)pos, 0);
        std::wstring pct = percent < 0 ? L"—" : (std::to_wstring(percent) + L"%");
        if (percent >= 0) any_valid = true;
        std::wstring info = pct + L"  重置 " +
                            U8ToW(parts[2]) + (parts[3] == "rate-limited" ? L"  【已限流】" : L"");
        SetText(IDC_Q_INFO1 + index, info);
        g_quota_last[index] = info;
        ++index;
    }
    if (index == 3 && any_valid) g_quota_have_last = true;
}

static void ShowQuotaError(const std::wstring& message) {
    SetHintAuto(message);
    if (g_quota_have_last) {
        for (int i = 0; i < 3; ++i) SetText(IDC_Q_INFO1 + i, L"⚠ 上次：" + g_quota_last[i]);
        return;
    }
    SetText(IDC_Q_INFO1, message);
    SetText(IDC_Q_INFO2, L"—");
    SetText(IDC_Q_INFO3, L"—");
    for (int i = 0; i < 3; ++i)
        SendMessageW(GetDlgItem(g_hwnd, IDC_Q_BAR1 + i), PBM_SETPOS, 0, 0);
}

static void HandleQuota(const Resp* r) {
    if (r->status == 200 && r->body.rfind("R ", 0) == 0) {
        RenderQuota(r->body);
    } else {
        ShowQuotaError(L"额度读取失败");
    }
}

static void HandleQuotaDirect(const Resp* r) {
    g_quota_busy = false;
    if (r->body.rfind("R ", 0) == 0) {
        RenderQuota(r->body);
        return;
    }
    std::string message = r->body;
    if (message.rfind("E ", 0) == 0) {
        auto parts = Split(message.substr(2), '|');
        message = parts.size() > 1 ? parts[1] : parts[0];
    }
    if (message == "未配置 API Key") g_quota_have_last = false;
    ShowQuotaError(U8ToW(message));
}

static void UpdateModelGroupTitle(bool failed) {
    HWND group = GetDlgItem(g_hwnd, IDC_MODEL_GROUP);
    if (!group) return;
    int imported = 0, free_count = 0;
    for (const auto& m : g_models) {
        if (m.imported) ++imported;
        if (m.free) ++free_count;
    }
    std::wstring title = L"模型";
    if (!g_models.empty()) {
        title += L" · " + std::to_wstring(imported) + L" 托管 / " +
                 std::to_wstring((int)g_models.size()) + L" 可选 / " +
                 std::to_wstring(free_count) + L" 免费";
    }
    if (failed && g_running) title += L" · 读取失败";
    SetWindowTextW(group, title.c_str());
    InvalidateRect(group, nullptr, TRUE);
}

static void HandleModels(const Resp* r) {
    if (r->status != 200 || r->body.rfind("M ok=1", 0) != 0) {
        UpdateModelGroupTitle(true);
        if (g_refresh_notice) {
            g_refresh_notice = false;
            SetHintSticky(L"刷新模型状态失败");
        } else {
            SetHintAuto(L"模型列表读取失败");
        }
        return;
    }
    std::map<std::string, std::string> probe_map;
    for (auto& old : g_models) {
        if (!old.probe.empty()) probe_map[old.id] = old.probe;
    }

    g_models.clear();
    for (const auto& line : Lines(r->body)) {
        if (line.rfind("M ", 0) != 0 || line.rfind("M ok=1", 0) == 0) continue;
        auto p = Split(line.substr(2), '|');
        if (p.size() < 8) continue;
        ModelRow m;
        m.id = p[0];
        m.name = p[1];
        m.ctx = atoll(p[2].c_str());
        m.out = atoll(p[3].c_str());
        m.think = p[4];
        m.efforts = p[5] == "-" ? "" : p[5];
        m.free = p[6] == "1";
        m.imported = p[7] == "1";
        m.training = p.size() > 8 && p[8] == "1";
        m.limited = p.size() > 9 && p[9] == "1";
        m.zdr = p.size() > 10 && p[10] == "1";
        auto hit = probe_map.find(m.id);
        if (hit != probe_map.end()) m.probe = hit->second;
        g_models.push_back(m);
    }
    std::stable_sort(g_models.begin(), g_models.end(), [](const ModelRow& a, const ModelRow& b) {
        return !a.probe.empty() && b.probe.empty();
    });
    FillModels();
    UpdateModelGroupTitle(false);
    if (g_refresh_notice) {
        g_refresh_notice = false;
        SetHintSticky(L"列表已刷新");
    }
}

static void HandleLog(const Resp* r) {
    if (r->status != 200 || r->body.empty()) return;
    std::wstring text = U8ToW(r->body);
    std::wstring crlf;
    crlf.reserve(text.size() + 256);
    for (wchar_t c : text) {
        if (c == L'\n') crlf += L"\r\n";
        else if (c != L'\r') crlf += c;
    }
    HWND log = GetDlgItem(g_hwnd, IDC_LOG);
    SetWindowTextW(log, crlf.c_str());
    SendMessageW(log, EM_SETSEL, (WPARAM)-2, (LPARAM)-1);
    SendMessageW(log, EM_SCROLLCARET, 0, 0);
}

static void HandleProbeStart(const Resp* r) {
    bool selftest = (g_job_kind == 1);
    if (r->status != 202) {
        g_probe_busy = false;
        SetHintSticky(r->status == 409 ? FriendlyError(r->body)
                                           : (selftest ? L"测试启动失败" : L"探测启动失败"));
        return;
    }
    auto kv = ParseKV(r->body);
    g_job_id = kv["id"];
    SetHintSticky(selftest ? L"测试中 ..." : L"探测中 ...");
    SetTimer(g_hwnd, 3, 800, nullptr);
}

static void HandleJob(const Resp* r) {
    auto lines = Lines(r->body);
    if (lines.empty()) return;
    auto kv = ParseKV(lines[0]);
    if (kv.find("state") == kv.end()) {
        SetHintSticky(U8ToW("job error: " + r->body.substr(0, 90)));
        g_probe_busy = false;
        g_job_id.clear();
        KillTimer(g_hwnd, 3);
        return;
    }
    bool selftest = kv["kind"] == "selftest";
    std::wstring progress = (selftest ? L"测试 " : L"探测 ") +
                            U8ToW(kv["done"]) + L"/" + U8ToW(kv["total"]);
    if (!kv["current"].empty() && kv["current"] != "-")
        progress += L" · " + U8ToW(kv["current"]);
    SetHintSticky(progress);

    std::vector<ProbeRow> results;
    std::wstring report;
    std::wstring job_error;
    int passed = 0, skipped = 0, failed = 0;
    for (size_t i = 1; i < lines.size(); ++i) {
        if (selftest && lines[i].rfind("E ", 0) == 0) {
            job_error = U8ToW(lines[i].substr(2));
            continue;
        }
        if (selftest && lines[i].rfind("T ", 0) == 0) {
            auto p = Split(lines[i].substr(2), '|');
            if (p.size() < 3) continue;
            bool ok = p[1] == "1";
            bool skip = p[2].rfind("跳过", 0) == 0;
            if (skip) ++skipped;
            else if (ok) ++passed;
            else ++failed;
            report += (skip ? L"[跳过] " : (ok ? L"[通过] " : L"[失败] ")) +
                      U8ToW(p[0]) + L"  " + U8ToW(p[2]) + L"\r\n";
            continue;
        }
        if (lines[i].rfind("F ", 0) != 0) continue;
        auto p = Split(lines[i].substr(2), '|');
        if (p.size() < 5) continue;
        ProbeRow row;
        row.id = p[0];
        row.route = p[1];
        row.status = atoi(p[2].c_str());
        row.kind = p[3];
        results.push_back(row);
    }
    if (!results.empty()) {
        g_probe = results;
        ApplyProbeToModels();
    }

    if (kv["state"] == "done" || kv["state"] == "error") {
        g_probe_busy = false;
        g_job_id.clear();
        KillTimer(g_hwnd, 3);
        if (selftest) {
            std::wstring title = L"测试结果 · " + U8ToW(kv["model"]);
            std::wstring header = L"模型 " + U8ToW(kv["model"]) +
                                  L" · 通过 " + std::to_wstring(passed) + L"/5";
            if (skipped > 0) header += L"（跳过 " + std::to_wstring(skipped) + L"）";
            if (!report.empty()) report = header + L"\r\n" + report;
            if (kv["state"] == "error") {
                report += L"\r\n任务异常：" +
                          (job_error.empty() ? L"服务端未返回错误文本" : job_error) + L"\r\n";
            }
            if (report.empty()) report = L"没有返回任何检查项。\r\n";
            UINT icon = (kv["state"] == "error" || failed > 0) ? MB_ICONWARNING
                                                               : MB_ICONINFORMATION;
            MessageBoxW(g_hwnd, report.c_str(), title.c_str(), icon);
            SetHintSticky(kv["state"] == "error" ? L"测试异常" : L"测试完成");
        } else {
            int ok = 0;
            for (auto& row : g_probe) if (row.score() > 0) ++ok;
            g_last_probe_summary = "探测完成：" + std::to_string(ok) + "/" + std::to_string(g_probe.size()) + " 可用";
            SetHintSticky(U8ToW(g_last_probe_summary));
        }
    }
}

static void HandleAction(const Resp* r) {
    bool sticky = g_action_sticky;
    g_action_sticky = false;
    auto notify = [&](const std::wstring& text) {
        if (sticky) SetHintSticky(text); else SetHintUser(text);
    };
    if (r->body.rfind("E ", 0) == 0) {
        if (g_restore_pending) {
            g_restore_pending = false;
            if (!IsWindowVisible(g_hwnd)) ShowPanel();
            std::wstring msg = FriendlyError(r->body);
            SetHintUser(msg);
            MessageBoxW(g_hwnd, msg.c_str(), L"恢复失败", MB_ICONWARNING);
            return;
        }
        if (g_proxy_pending) {
            g_proxy_pending = false;
            if (!IsWindowVisible(g_hwnd)) ShowPanel();
            ShowUrlError(FriendlyError(r->body));
            return;
        }
        if (g_purge_fallback) {
            g_purge_fallback = false;
            StartLocalPurge();
            return;
        }
        std::wstring msg = FriendlyError(r->body);
        notify(msg);
        MessageBoxW(g_hwnd, msg.c_str(), L"操作失败", MB_ICONWARNING);
        return;
    }
    if (r->body.rfind("M ", 0) == 0) {
        auto kv = ParseKV(r->body);
        g_restore_pending = false;
        std::wstring msg = L"已恢复 models.json";
        if (!kv["restored"].empty()) msg += L"：" + U8ToW(kv["restored"]) + L" 条";
        SetHintUser(msg);
        RefreshModelsAsync();
        RefreshStatusAsync();
        return;
    }
    if (r->body.rfind("K ", 0) == 0) {
        auto kv = ParseKV(r->body);
        std::string msg = "密钥已更新，改动 " + kv["models"] + " 条，月额度 " + kv["monthly"] + "%";
        notify(U8ToW(msg));
        SetWindowTextW(GetDlgItem(g_hwnd, IDC_KEY), L"");
        RefreshModelsAsync();
        RefreshQuotaAsync();
    } else if (r->body.rfind("R ", 0) == 0) {
        auto kv = ParseKV(r->body);
        std::string msg = "重装完成：写入 " + kv["written"] + " 条，保留 " + kv["kept"] +
                          " 条自有，共 " + kv["rows"] + " 条";
        if (atoi(kv["conflicts"].c_str()) > 0) {
            std::wstring ids = U8ToW(kv["ids"]);
            msg += "；" + kv["conflicts"] + " 个 id 冲突未写入";
            if (!IsWindowVisible(g_hwnd)) ShowPanel();
            MessageBoxW(g_hwnd,
                        (U8ToW(msg) + L"\n\n冲突 id：\n" + ids).c_str(),
                        L"ID 冲突", MB_ICONWARNING);
        }
        notify(U8ToW(msg));
        RefreshModelsAsync();
    } else if (r->body.rfind("P ", 0) == 0) {
        g_purge_fallback = false;
        auto kv = ParseKV(r->body);
        std::string msg = "已清空 " + kv["purged"] + " 条，保留 " + kv["kept"] + " 条自有模型";
        notify(U8ToW(msg));
        RefreshModelsAsync();
    } else if (r->body.rfind("U ", 0) == 0) {
        auto kv = ParseKV(r->body);
        g_proxy_dirty = false;
        g_proxy_pending = false;
        ClearUrlError();
        g_proxy_applied = kv["mode"];
        g_proxy_mode = U8ToW(kv["mode"]);
        if (!kv["url"].empty()) g_proxy_url_synced = U8ToW(kv["url"]);
        if (!kv["url"].empty()) g_proxy_url_current = U8ToW(kv["url"]);
        notify(L"上游代理已切换");
        UpdateTrayTip();
    } else if (r->body.rfind("S ", 0) == 0) {
        notify(L"转发桥已停止");
    }
    RefreshStatusAsync();
}

static void OnHttp(WPARAM kind, Resp* r) {
    switch (kind) {
        case K_STATUS: HandleStatus(r); break;
        case K_QUOTA: HandleQuota(r); break;
        case K_QUOTA_DIRECT: HandleQuotaDirect(r); break;
        case K_MODELS: HandleModels(r); break;
        case K_LOG: HandleLog(r); break;
        case K_PROBE_START: HandleProbeStart(r); break;
        case K_JOB: HandleJob(r); break;
        case K_ACTION: HandleAction(r); break;
        default: break;
    }
    delete r;
}


// --------------------------------------------------------------------------- //
// tray icon
// --------------------------------------------------------------------------- //

enum {
    IDM_TRAY_SHOW = 4001,
    IDM_TRAY_TOGGLE = 4002,
    IDM_TRAY_OFF = 4011,
    IDM_TRAY_AUTO = 4012,
    IDM_TRAY_MANUAL = 4013,
    IDM_TRAY_EXIT_STOP = 4022
};

static void ShowPanel() {
    if (!g_hwnd) return;
    ShowWindow(g_hwnd, IsIconic(g_hwnd) ? SW_RESTORE : SW_SHOW);
    SetForegroundWindow(g_hwnd);
}

static void TogglePanel() {
    if (!g_hwnd) return;
    if (IsWindowVisible(g_hwnd)) {
        ShowWindow(g_hwnd, SW_HIDE);
        return;
    }
    ShowPanel();
}

static void TrayRemove() {
    if (!g_tray_added) return;
    Shell_NotifyIconW(NIM_DELETE, &g_nid);
    g_tray_added = false;
}

static void UpdateTrayTip() {
    if (!g_tray_added) return;
    std::wstring tip = L"OCSub2WB · ";
    if (!g_running) {
        tip += L"转发桥未运行";
    } else {
        tip += L"运行中 · 端口 " + g_status_port + L" · 代理 ";
        if (g_proxy_mode == L"off") {
            tip += L"直连";
        } else if (g_proxy_mode == L"auto") {
            tip += L"自动";
            if (!g_proxy_url_current.empty()) {
                std::wstring shown = g_proxy_url_current;
                if (shown.rfind(L"http://", 0) == 0) shown = shown.substr(7);
                tip += L"(" + shown + L")";
            }
        } else {
            tip += L"手动";
            if (!g_proxy_url_current.empty()) {
                std::wstring shown = g_proxy_url_current;
                if (shown.rfind(L"http://", 0) == 0) shown = shown.substr(7);
                tip += L"→" + shown;
            }
        }
    }
    if (tip == g_tray_tip_last) return;
    NOTIFYICONDATAW nid = g_nid;
    nid.uFlags = NIF_TIP | NIF_SHOWTIP;
    lstrcpynW(nid.szTip, tip.c_str(), 128);
    if (Shell_NotifyIconW(NIM_MODIFY, &nid)) g_tray_tip_last = tip;
}

static void TrayAdd() {
    if (!g_hwnd || g_tray_added) return;
    if (!g_icon_small) {
        g_icon_small = (HICON)LoadImageW(GetModuleHandleW(nullptr), MAKEINTRESOURCEW(1),
                                         IMAGE_ICON, GetSystemMetrics(SM_CXSMICON),
                                         GetSystemMetrics(SM_CYSMICON), LR_DEFAULTCOLOR);
    }
    ZeroMemory(&g_nid, sizeof(g_nid));
    g_nid.cbSize = sizeof(g_nid);
    g_nid.hWnd = g_hwnd;
    g_nid.uID = 1;
    g_nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP | NIF_SHOWTIP;
    g_nid.uCallbackMessage = WM_APP_TRAY;
    g_nid.hIcon = g_icon_small ? g_icon_small : g_icon_large;
    lstrcpynW(g_nid.szTip, L"OCSub2WB", 128);
    if (!Shell_NotifyIconW(NIM_ADD, &g_nid)) return;
    g_nid.uVersion = NOTIFYICON_VERSION_4;
    Shell_NotifyIconW(NIM_SETVERSION, &g_nid);
    g_tray_added = true;
    g_tray_tip_last.clear();
    UpdateTrayTip();
}

static void TrayBalloon() {
    if (!g_tray_added || g_balloon_shown) return;
    g_balloon_shown = true;
    NOTIFYICONDATAW nid = g_nid;
    nid.uFlags = NIF_INFO;
    nid.dwInfoFlags = NIIF_INFO;
    nid.uTimeout = 10000;
    lstrcpynW(nid.szInfoTitle, L"OCSub2WB", 64);
    lstrcpynW(nid.szInfo, L"已收起到托盘，单击图标可重新打开", 256);
    Shell_NotifyIconW(NIM_MODIFY, &nid);
}

static void TraySetProxy(const std::wstring& mode) {
    if (mode == g_proxy_mode) return;
    std::string value = WToU8(mode);
    std::string url = WToU8(g_proxy_url_current);
    if (value != "off") {
        std::string host;
        int port = 0;
        if (url.empty() || !ParseProxyUrl(url, host, port)) {
            ShowPanel();
            SetFocus(GetDlgItem(g_hwnd, IDC_PROXY_URL));
            ShowUrlError(L"地址不合法，请先在面板填写 http://IPv4:端口");
            return;
        }
    }
    ApplyProxyMode(value, url);
}

static void TrayMenu() {
    SetForegroundWindow(g_hwnd);
    HMENU menu = CreatePopupMenu();
    if (!menu) return;
    AppendMenuW(menu, MF_STRING, IDM_TRAY_SHOW,
                IsWindowVisible(g_hwnd) ? L"隐藏面板" : L"打开面板");
    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(menu, MF_STRING, IDM_TRAY_TOGGLE,
                g_running ? L"停止转发桥" : L"启动转发桥");

    HMENU proxy = CreatePopupMenu();
    AppendMenuW(proxy, MF_STRING | (g_proxy_mode == L"off" ? MF_CHECKED : 0),
                IDM_TRAY_OFF, L"直连");
    AppendMenuW(proxy, MF_STRING | (g_proxy_mode == L"auto" ? MF_CHECKED : 0),
                IDM_TRAY_AUTO, L"自动（仅锁区走代理）");
    AppendMenuW(proxy, MF_STRING | (g_proxy_mode == L"manual" ? MF_CHECKED : 0),
                IDM_TRAY_MANUAL, L"手动（全部走代理）");
    AppendMenuW(menu, MF_POPUP, (UINT_PTR)proxy, L"代理模式");

    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(menu, MF_STRING, IDM_TRAY_EXIT_STOP, L"退出并停止转发桥");

    POINT pt;
    GetCursorPos(&pt);
    int cmd = (int)TrackPopupMenu(menu,
                                  TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                                  pt.x, pt.y, 0, g_hwnd, nullptr);
    PostMessageW(g_hwnd, WM_NULL, 0, 0);
    DestroyMenu(menu);

    switch (cmd) {
        case IDM_TRAY_SHOW: TogglePanel(); break;
        case IDM_TRAY_TOGGLE: DoStartStop(); break;
        case IDM_TRAY_OFF: TraySetProxy(L"off"); break;
        case IDM_TRAY_AUTO: TraySetProxy(L"auto"); break;
        case IDM_TRAY_MANUAL: TraySetProxy(L"manual"); break;
        case IDM_TRAY_EXIT_STOP:
            ShowPanel();
            DoExit();
            break;
        default: break;
    }
}

// --------------------------------------------------------------------------- //
// window proc
// --------------------------------------------------------------------------- //

static SIZE MinimumWindowSize() {
    RECT rect = {0, 0, CLIENT_W, CLIENT_H};
    AdjustWindowRect(&rect, PANEL_STYLE, FALSE);
    return {rect.right - rect.left, rect.bottom - rect.top};
}

static void FitNormalWindowToWorkArea(HWND hwnd) {
    if (IsIconic(hwnd) || IsZoomed(hwnd)) return;

    RECT rect = {};
    MONITORINFO monitor = {sizeof(monitor)};
    if (!GetWindowRect(hwnd, &rect) ||
        !GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), &monitor)) return;

    const RECT& work = monitor.rcWork;
    const SIZE minimum = MinimumWindowSize();
    const LONG width = std::clamp(rect.right - rect.left, minimum.cx,
                                  std::max(minimum.cx, work.right - work.left));
    const LONG height = std::clamp(rect.bottom - rect.top, minimum.cy,
                                   std::max(minimum.cy, work.bottom - work.top));
    // If the work area is smaller than the minimum, keep the caption buttons reachable.
    const LONG x = width > work.right - work.left
                 ? work.right - width : std::clamp(rect.left, work.left, work.right - width);
    const LONG y = std::clamp(rect.top, work.top, std::max(work.top, work.bottom - height));

    if (x != rect.left || y != rect.top || width != rect.right - rect.left ||
        height != rect.bottom - rect.top) {
        SetWindowPos(hwnd, nullptr, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE);
    }
}

static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    static bool was_minimized_or_maximized = false;
    static int last_client_width = CLIENT_W;
    if (g_taskbar_created && msg == g_taskbar_created) {
        TrayRemove();
        TrayAdd();
        return 0;
    }
    switch (msg) {
        case WM_CREATE:
            g_hwnd = hwnd;
            CreateControls();
            LoadToken();
            SetTimer(hwnd, 1, 2000, nullptr);
            SetTimer(hwnd, 2, 6000, nullptr);
            SetTimer(hwnd, 4, 60000, nullptr);
            SetTimer(hwnd, 5, 80, nullptr);
            g_tip = CreateWindowExW(WS_EX_TOPMOST, TOOLTIPS_CLASSW, nullptr,
                                    WS_POPUP | TTS_NOPREFIX | TTS_ALWAYSTIP,
                                    0, 0, 0, 0, hwnd, nullptr, GetModuleHandleW(nullptr), nullptr);
            if (g_tip) {
                TOOLINFOW ti = {};
                ti.cbSize = sizeof(ti);
                ti.uFlags = TTF_TRACK | TTF_ABSOLUTE;
                ti.hwnd = hwnd;
                ti.uId = 1;
                ti.lpszText = (LPWSTR)L"需启动转发桥";
                SendMessageW(g_tip, TTM_ADDTOOLW, 0, (LPARAM)&ti);
                SendMessageW(g_tip, TTM_SETMAXTIPWIDTH, 0, 320);
            }
            TrayAdd();
            RefreshStatusAsync();
            RefreshModelsAsync();
            RefreshQuotaAsync();
            RefreshLogAsync();
            return 0;

        case WM_GETMINMAXINFO: {
            const SIZE minimum = MinimumWindowSize();
            auto* info = reinterpret_cast<MINMAXINFO*>(lp);
            info->ptMinTrackSize.x = minimum.cx;
            info->ptMinTrackSize.y = minimum.cy;
            info->ptMaxTrackSize.x = std::max(info->ptMaxTrackSize.x, minimum.cx);
            info->ptMaxTrackSize.y = std::max(info->ptMaxTrackSize.y, minimum.cy);
            return 0;
        }

        case WM_SIZE:
            if (wp == SIZE_RESTORED || wp == SIZE_MAXIMIZED) {
                const int width = LOWORD(lp);
                ResizeControls(width, HIWORD(lp));
                if (width != last_client_width) {
                    AutoSizeColumns();
                    ResizeRecentRequestColumns();
                    last_client_width = width;
                }
                if (wp == SIZE_RESTORED && was_minimized_or_maximized)
                    PostMessageW(hwnd, WM_APP_FIT_WINDOW, 0, 0);
            }
            if (wp == SIZE_RESTORED || wp == SIZE_MAXIMIZED || wp == SIZE_MINIMIZED)
                was_minimized_or_maximized = wp != SIZE_RESTORED;
            return 0;

        case WM_DISPLAYCHANGE:
        case WM_SETTINGCHANGE:
            if (msg == WM_DISPLAYCHANGE || wp == SPI_SETWORKAREA)
                PostMessageW(hwnd, WM_APP_FIT_WINDOW, 0, 0);
            break;

        case WM_APP_FIT_WINDOW:
            FitNormalWindowToWorkArea(hwnd);
            return 0;

        case WM_TIMER:
            if (wp == 1) RefreshStatusAsync();
            else if (wp == 2) RefreshLogAsync();
            else if (wp == 3 && !g_job_id.empty())
                PostHttp(K_JOB, L"GET", L"/api/job/" + U8ToW(g_job_id), "", 4000);
            else if (wp == 4) RefreshQuotaAsync();
            else if (wp == 5) UpdateRefreshTip();
            return 0;

        case WM_APP_HTTP:
            OnHttp(wp, (Resp*)lp);
            return 0;

        case WM_APP_JOB_DONE: {
            std::string* body = (std::string*)lp;
            HandleLocalPurge(*body);
            delete body;
            return 0;
        }

        case WM_APP_PROXY_CHECK:
            HandleProxyCheck((ProxyCheckResult*)lp);
            return 0;

        case WM_APP_TRAY: {
            UINT event = LOWORD(lp);
            if (event == NIN_SELECT || event == NIN_KEYSELECT) {
                ShowPanel();
            } else if (event == WM_CONTEXTMENU || event == WM_RBUTTONUP) {
                TrayMenu();
            }
            return 0;
        }

        case WM_APP_SHOW:
            ShowPanel();
            return 0;

        case WM_APP_EXIT_NOW:
            g_really_exit = true;
            DestroyWindow(hwnd);
            return 0;

        case WM_NOTIFY: {
            NMHDR* nh = (NMHDR*)lp;
            if (nh->idFrom == IDC_MODELS && nh->code == NM_CUSTOMDRAW) {
                LPNMLVCUSTOMDRAW cd = (LPNMLVCUSTOMDRAW)lp;
                if (cd->nmcd.dwDrawStage == CDDS_PREPAINT) {
                    return g_sel_active ? CDRF_NOTIFYPOSTPAINT : CDRF_DODEFAULT;
                }
                if (cd->nmcd.dwDrawStage == CDDS_POSTPAINT) {
                    if (g_sel_active) DrawTextSelection(cd->nmcd.hdc);
                    return CDRF_DODEFAULT;
                }
                return CDRF_DODEFAULT;
            }
            if (nh->idFrom == IDC_MODELS && nh->code == LVN_COLUMNCLICK) {
                int col = ((NMLISTVIEW*)lp)->iSubItem;
                if (col == g_sort_col) {
                    g_sort_asc = !g_sort_asc;
                } else {
                    g_sort_col = col;
                    g_sort_asc = true;
                }
                FillModels();
                return 0;
            }
            break;
        }

        case WM_CTLCOLOREDIT: {
            HWND child = (HWND)lp;
            if (child && GetDlgCtrlID(child) == IDC_PROXY_URL && g_proxy_url_invalid) {
                HDC dc = (HDC)wp;
                SetTextColor(dc, RGB(170, 0, 0));
                SetBkColor(dc, RGB(255, 232, 232));
                if (!g_invalid_brush) g_invalid_brush = CreateSolidBrush(RGB(255, 232, 232));
                return (LRESULT)g_invalid_brush;
            }
            break;
        }

        case WM_COMMAND: {
            int id = LOWORD(wp);
            WORD code = HIWORD(wp);
            if (code == BN_CLICKED &&
                (id == IDC_RADIO_OFF || id == IDC_RADIO_AUTO || id == IDC_RADIO_MANUAL)) {
                OnProxyRadio(id);
                return 0;
            }
            if (code == EN_CHANGE && id == IDC_PROXY_URL) {
                if (g_proxy_syncing) return 0;
                ClearUrlError();
                if (Button_GetCheck(GetDlgItem(g_hwnd, IDC_RADIO_OFF)) == BST_CHECKED)
                    return 0;
                wchar_t buf[512] = {};
                GetWindowTextW(GetDlgItem(g_hwnd, IDC_PROXY_URL), buf, 512);
                if (std::wstring(buf) == g_proxy_url_synced) return 0;
                g_proxy_dirty = true;
                return 0;
            }
            switch (id) {
                case IDC_BTN_START: DoStartStop(); break;
                case IDC_BTN_DIR:
                    ShellExecuteW(hwnd, L"open", (g_exe_dir + L"\\cache").c_str(),
                                  nullptr, nullptr, SW_SHOWNORMAL);
                    break;
                case IDC_BTN_EXIT: DoExit(); break;
                case IDC_Q_REFRESH: RefreshQuotaAsync(); break;
                case IDC_BTN_REFRESH: DoRefreshModels(); break;
                case IDC_BTN_PROBE: DoProbe(); break;
                case IDC_BTN_IMPORT: DoImportProbe(); break;
                case IDC_BTN_SELFTEST: DoSelftest(); break;
                case IDC_BTN_PURGE: DoPurge(); break;
                case IDC_BTN_REFRESH_LIST:
                    g_refresh_notice = true;
                    RefreshModelsAsync();
                    break;
                case IDC_BTN_KEY: DoSaveKey(); break;
                case IDC_BTN_PROXY: DoApplyProxy(); break;
                case IDM_TRAY_SHOW: TogglePanel(); break;
                case IDM_TRAY_TOGGLE: DoStartStop(); break;
                case IDM_TRAY_OFF: TraySetProxy(L"off"); break;
                case IDM_TRAY_AUTO: TraySetProxy(L"auto"); break;
                case IDM_TRAY_MANUAL: TraySetProxy(L"manual"); break;
                case IDM_TRAY_EXIT_STOP:
                    ShowPanel();
                    DoExit();
                    break;
                default: break;
            }
            return 0;
        }

        case WM_CLOSE:
            if (g_really_exit) {
                DestroyWindow(hwnd);
                return 0;
            }
            if (!g_tray_added) {
                DestroyWindow(hwnd);
                return 0;
            }
            ShowWindow(hwnd, SW_HIDE);
            TrayBalloon();
            UpdateTrayTip();
            return 0;

        case WM_QUERYENDSESSION:
            return TRUE;

        case WM_ENDSESSION:
            if (!wp) g_really_exit = false;
            return 0;

        case WM_DESTROY:
            TrayRemove();
            KillTimer(hwnd, 1);
            KillTimer(hwnd, 2);
            KillTimer(hwnd, 3);
            KillTimer(hwnd, 4);
            KillTimer(hwnd, 5);
            if (g_tip) { DestroyWindow(g_tip); g_tip = nullptr; }
            PostQuitMessage(0);
            return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE, PWSTR, int show) {
    INITCOMMONCONTROLSEX icc = { sizeof(icc), ICC_LISTVIEW_CLASSES | ICC_PROGRESS_CLASS | ICC_STANDARD_CLASSES | ICC_WIN95_CLASSES };
    InitCommonControlsEx(&icc);

    WSADATA wsa = {};
    WSAStartup(MAKEWORD(2, 2), &wsa);

    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring path = exe;
    size_t pos = path.find_last_of(L'\\');
    g_exe_dir = pos == std::wstring::npos ? L"." : path.substr(0, pos);

    NONCLIENTMETRICSW ncm = { sizeof(ncm) };
    SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, sizeof(ncm), &ncm, 0);
    LOGFONTW lf = ncm.lfMessageFont;
    lf.lfHeight = -13;
    wcscpy(lf.lfFaceName, L"Microsoft YaHei UI");
    g_font = CreateFontIndirectW(&lf);

    WNDCLASSEXW wc = { sizeof(wc) };
    wc.lpfnWndProc = WndProc;
    wc.hInstance = inst;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = L"OpenCodeGoPanel";
    g_icon_large = (HICON)LoadImageW(inst, MAKEINTRESOURCEW(1), IMAGE_ICON,
                                    GetSystemMetrics(SM_CXICON), GetSystemMetrics(SM_CYICON),
                                    LR_DEFAULTCOLOR);
    g_icon_small = (HICON)LoadImageW(inst, MAKEINTRESOURCEW(1), IMAGE_ICON,
                                    GetSystemMetrics(SM_CXSMICON), GetSystemMetrics(SM_CYSMICON),
                                    LR_DEFAULTCOLOR);
    if (!g_icon_large) g_icon_large = LoadIconW(nullptr, IDI_APPLICATION);
    if (!g_icon_small) g_icon_small = g_icon_large;
    wc.hIcon = g_icon_large;
    wc.hIconSm = g_icon_small;
    RegisterClassExW(&wc);

    HANDLE mutex = CreateMutexW(nullptr, TRUE, L"Local\\OpenCodeGoPanel.SingleInstance");
    if (mutex && GetLastError() == ERROR_ALREADY_EXISTS) {
        HWND existing = FindWindowW(L"OpenCodeGoPanel", nullptr);
        if (existing) PostMessageW(existing, WM_APP_SHOW, 0, 0);
        return 0;
    }

    const SIZE minimum = MinimumWindowSize();
    HWND hwnd = CreateWindowExW(
        0, L"OpenCodeGoPanel", L"OCSub2WB", PANEL_STYLE,
        CW_USEDEFAULT, CW_USEDEFAULT, minimum.cx, minimum.cy,
        nullptr, nullptr, inst, nullptr);
    if (!hwnd) return 1;

    ShowWindow(hwnd, show);
    FitNormalWindowToWorkArea(hwnd);
    UpdateWindow(hwnd);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    WSACleanup();
    if (g_font) DeleteObject(g_font);
    return 0;
}
