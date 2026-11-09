// harness_cpp_gpu_model.cu: the published runner plumbing of harness_cpp_gpu (host code only).
// Defines every operation the host-rendered header harness_cpp_gpu_model.cuh declares.
// harness_cpp_gpu_model.cu: a test fixture for the host-rendered driver (issue #589 PR-1b), NOT a
// certified source: the certified harness_cpp_gpu@0.2.0 (2026-10-29) emitters and writers, its
// retired writers removed, with hand-written reading operations (the certified @0.4.0 is generated).
#include "harness_cpp_gpu_model.cuh"

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <cctype>
#include <filesystem>
#include <limits>
#include <map>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace harness_cpp_gpu_model {

namespace {
std::string kept_spec_path;
bool spec_kept = false;


// JSON string literal of s (quotes, backslash and control characters escaped).
std::string json_string(const std::string& s) {
    std::string out = "\"";
    for (const char c : s) {
        const unsigned char u = static_cast<unsigned char>(c);
        if (c == '"') {
            out += "\\\"";
        } else if (c == '\\') {
            out += "\\\\";
        } else if (u < 0x20U) {
            char buf[8];
            std::snprintf(buf, sizeof buf, "\\u%04x", static_cast<unsigned int>(u));
            out += buf;
        } else {
            out += c;
        }
    }
    out += "\"";
    return out;
}

// Writes text to the relative path; a failure the runtime reports stops the program.
void write_text_file(const std::string& path, const std::string& text) {
    std::ofstream f(path, std::ios::out | std::ios::trunc | std::ios::binary);
    if (!f) {
        std::cerr << "harness_cpp_gpu: cannot open " << path << " for writing\n";
        std::exit(1);
    }
    f << text;
    f.close();
    if (!f) {
        std::cerr << "harness_cpp_gpu: write to " << path << " failed\n";
        std::exit(1);
    }
}

void ensure_directory(const std::string& dir) {
    std::error_code ec;
    std::filesystem::create_directories(dir, ec);
    if (ec) {
        std::cerr << "harness_cpp_gpu: cannot create directory " << dir << ": " << ec.message() << "\n";
        std::exit(1);
    }
}

// Nested JSON emission of a column-major array, the LEADING index outermost.
// `dim` is the index being expanded, `offset` the column-major offset fixed so far.
std::string emit_nested(const dp* data, const long* extent, const long* stride, int rank, int dim, long offset) {
    std::string s = "[";
    for (long i = 0; i < extent[dim]; ++i) {
        if (i > 0) {
            s += ", ";
        }
        const long off = offset + i * stride[dim];
        if (dim == rank - 1) {
            s += harness_cpp_gpu__emit_real(data[off]);
        } else {
            s += emit_nested(data, extent, stride, rank, dim + 1, off);
        }
    }
    s += "]";
    return s;
}

template <int R>
std::string emit_view(const atmofab::View<const dp, R>& a) {
    long stride[R];
    stride[0] = 1;
    for (int d = 1; d < R; ++d) {
        stride[d] = stride[d - 1] * a.extent[d - 1];
    }
    return emit_nested(a.data, a.extent, stride, R, 0, 0);
}

// Trailing blanks removed ('na  ' -> "na").

}  // namespace

void harness_cpp_gpu__parse_cases(
    const std::vector<std::string>& tokens,
    int ntokens,
    std::vector<std::string>& case_ids,
    int& ncases,
    bool& ok) {
    int flag_at = -1;
    for (int i = 0; i < ntokens; ++i) {
        if (tokens[static_cast<std::size_t>(i)] == "--cases") {
            flag_at = i;
            break;
        }
    }
    case_ids.clear();
    ncases = 0;
    if (flag_at < 0 || flag_at + 1 >= ntokens) {
        ok = false;
        return;
    }
    // The ONE token after "--cases" is the spec path (kept, §3.2); the rest are case ids.
    bool too_long = false;
    for (int i = flag_at + 2; i < ntokens; ++i) {
        const std::string& id = tokens[static_cast<std::size_t>(i)];
        if (id.size() > static_cast<std::size_t>(case_id_len)) {
            too_long = true;
        }
        case_ids.push_back(id);
    }
    ncases = static_cast<int>(case_ids.size());
    ok = (ncases > 0) && !too_long;
    if (ok) {
        kept_spec_path = tokens[static_cast<std::size_t>(flag_at + 1)];
        spec_kept = true;
    }
}

std::string harness_cpp_gpu__emit_real(
    dp x) {
    if (std::isnan(x)) {
        return "NaN";
    }
    if (std::isinf(x)) {
        return x > 0 ? "Infinity" : "-Infinity";
    }
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.16e", x);
    return buf;
}

std::string harness_cpp_gpu__emit_int(
    int i) {
    return std::to_string(i);
}

std::string harness_cpp_gpu__emit_bool(
    bool b) {
    return b ? "true" : "false";
}

std::string harness_cpp_gpu__emit_array_r1(
    atmofab::View<const dp, 1> a) {
    return emit_view<1>(a);
}

std::string harness_cpp_gpu__emit_array_r2(
    atmofab::View<const dp, 2> a) {
    return emit_view<2>(a);
}

std::string harness_cpp_gpu__emit_array_r3(
    atmofab::View<const dp, 3> a) {
    return emit_view<3>(a);
}

std::string harness_cpp_gpu__emit_array_r4(
    atmofab::View<const dp, 4> a) {
    return emit_view<4>(a);
}

harness_cpp_gpu__h_named harness_cpp_gpu__box(
    const std::string& name,
    const std::string& json) {
    harness_cpp_gpu__h_named nv;
    nv.name = name;
    nv.json = json;
    return nv;
}

void harness_cpp_gpu__write_snapshot(
    const std::string& case_id,
    const std::vector<harness_cpp_gpu__h_named>& values,
    dp time) {
    const std::string dir = "raw/state_snapshots";
    ensure_directory(dir);
    const std::string path = dir + "/" + case_id + ".json";
    std::string doc = "{\"t\": " + harness_cpp_gpu__emit_real(time);
    for (const harness_cpp_gpu__h_named& v : values) {
        doc += ", " + json_string(v.name) + ": " + v.json;
    }
    doc += "}\n";
    write_text_file(path, doc);
}

void harness_cpp_gpu__write_perf(
    const std::string& case_id,
    const std::string& target,
    int steps,
    int cells_updated,
    dp walltime_sec,
    int mpi_ranks,
    int threads_per_rank,
    int gpu_devices) {
    const dp throughput = static_cast<dp>(cells_updated) / walltime_sec;
    const int parallel_degree_total = mpi_ranks * threads_per_rank * (gpu_devices > 1 ? gpu_devices : 1);

    const std::time_t now = std::time(nullptr);
    char stamp[32];
    std::strftime(stamp, sizeof stamp, "%Y-%m-%dT%H:%M:%SZ", std::gmtime(&now));

    const std::string doc =
        "{\n  \"case_id\": " + json_string(case_id) + ",\n  \"target\": " + json_string(target) +
        ",\n  \"walltime_sec\": " + harness_cpp_gpu__emit_real(walltime_sec) +
        ",\n  \"steps\": " + harness_cpp_gpu__emit_int(steps) +
        ",\n  \"cells_updated\": " + harness_cpp_gpu__emit_int(cells_updated) +
        ",\n  \"throughput_cells_per_sec\": " + harness_cpp_gpu__emit_real(throughput) +
        ",\n  \"parallelism\": {\"mpi_ranks\": " + harness_cpp_gpu__emit_int(mpi_ranks) +
        ", \"threads_per_rank\": " + harness_cpp_gpu__emit_int(threads_per_rank) +
        ", \"gpu_devices\": " + harness_cpp_gpu__emit_int(gpu_devices) +
        ", \"parallel_degree_total\": " + harness_cpp_gpu__emit_int(parallel_degree_total) +
        ", \"block_size\": " + harness_cpp_gpu__emit_int(harness_cpp_gpu__block_size()) +
        "},\n  \"timestamp_utc\": " + json_string(stamp) + "\n}\n";
    write_text_file("perf.json", doc);
}

// --- The reading operations (issue #589). A hand-written realization of harness_cpp_gpu@0.4.0
// §3.2 for the driver's tests, not a certified source: the file is one flat JSON object.
namespace {
struct CaseEntry {
    int depth = 0;
    std::vector<long> extent;
    std::vector<dp> flat;
    bool integer_token = false;
    int integer_value = 0;
    bool bool_token = false;
    bool bool_value = false;
};
std::map<std::string, CaseEntry> loaded_case;
bool case_loaded = false;

[[noreturn]] void refuse_read(const std::string& name, const char* what) {
    std::cerr << "error: case value " << name << ": " << what << std::endl;
    std::exit(2);
}

struct Parser {
    const std::string& s;
    std::size_t at = 0;
    bool ok = true;

    void blanks() {
        while (at < s.size() && (s[at] == ' ' || s[at] == '\n' || s[at] == '\t' || s[at] == '\r')) {
            ++at;
        }
    }
    bool eat(char c) {
        blanks();
        if (at < s.size() && s[at] == c) {
            ++at;
            return true;
        }
        return false;
    }
    std::string key() {
        blanks();
        std::string out;
        if (at >= s.size() || s[at] != '"') {
            ok = false;
            return out;
        }
        ++at;
        while (at < s.size() && s[at] != '"') {
            if (s[at] == '\\') {
                ok = false;
                return out;
            }
            out.push_back(s[at++]);
        }
        if (at >= s.size()) {
            ok = false;
            return out;
        }
        ++at;
        return out;
    }
    bool number(CaseEntry& e) {
        blanks();
        const std::size_t start = at;
        while (at < s.size() && (std::isdigit(static_cast<unsigned char>(s[at])) || s[at] == '-'
                                 || s[at] == '+' || s[at] == '.' || s[at] == 'e' || s[at] == 'E')) {
            ++at;
        }
        if (start == at) {
            return false;
        }
        const std::string token = s.substr(start, at - start);
        char* end = nullptr;
        const double x = std::strtod(token.c_str(), &end);
        if (*end != '\0' || !std::isfinite(x)) {
            return false;
        }
        e.flat.push_back(x);
        if (token.find_first_of(".eE") == std::string::npos) {
            const long long i = std::strtoll(token.c_str(), nullptr, 10);
            if (i >= std::numeric_limits<int>::min() && i <= std::numeric_limits<int>::max()) {
                e.integer_token = true;
                e.integer_value = static_cast<int>(i);
            }
        }
        if (token == "1" || token == "0") {
            e.bool_token = true;
            e.bool_value = token == "1";
        }
        return true;
    }
    bool value(CaseEntry& e, int depth) {
        blanks();
        if (at < s.size() && s[at] == '[') {
            ++at;
            if (depth >= 4) {
                return false;
            }
            if (static_cast<int>(e.extent.size()) <= depth) {
                e.extent.resize(static_cast<std::size_t>(depth) + 1, -1);
            }
            long n = 0;
            if (!eat(']')) {
                do {
                    if (!value(e, depth + 1)) {
                        return false;
                    }
                    ++n;
                } while (eat(','));
                if (!eat(']')) {
                    return false;
                }
            }
            long& extent = e.extent[static_cast<std::size_t>(depth)];
            if (extent >= 0 && extent != n) {
                return false;
            }
            extent = n;
            return true;
        }
        return number(e);
    }
};

const CaseEntry& lookup(const std::string& name) {
    if (!case_loaded) {
        refuse_read(name, "no case is loaded");
    }
    const auto found = loaded_case.find(name);
    if (found == loaded_case.end()) {
        refuse_read(name, "the case does not hold it");
    }
    return found->second;
}

const CaseEntry& scalar_entry(const std::string& name) {
    const CaseEntry& e = lookup(name);
    if (!e.extent.empty()) {
        refuse_read(name, "an array asked as a scalar");
    }
    return e;
}

// The file's leading index is the outermost level; the view's first index is the fastest.
template <int R>
void array_entry(const std::string& name, std::vector<dp>& out, long* extent) {
    const CaseEntry& e = lookup(name);
    // An empty array gives extent 0 at its level and every level below it.
    const bool stops_empty = !e.extent.empty() && e.extent.back() == 0;
    if (static_cast<int>(e.extent.size()) > R
        || (static_cast<int>(e.extent.size()) < R && !stops_empty)) {
        refuse_read(name, "a value of another nesting depth");
    }
    long total = 1;
    for (int k = 0; k < R; ++k) {
        extent[k] = k < static_cast<int>(e.extent.size())
                        ? e.extent[static_cast<std::size_t>(k)] : 0;
        total *= extent[k];
    }
    out.assign(static_cast<std::size_t>(total), 0.0);
    std::vector<long> idx(R, 0);
    for (long p = 0; p < total; ++p) {
        long pos = 0;
        long stride = 1;
        for (int k = 0; k < R; ++k) {
            pos += idx[static_cast<std::size_t>(k)] * stride;
            stride *= extent[k];
        }
        out[static_cast<std::size_t>(pos)] = e.flat[static_cast<std::size_t>(p)];
        for (int k = R - 1; k >= 0; --k) {
            if (++idx[static_cast<std::size_t>(k)] < extent[k]) {
                break;
            }
            idx[static_cast<std::size_t>(k)] = 0;
        }
    }
}
}  // namespace

void harness_cpp_gpu__read_case(const std::string& case_id, bool& ok) {
    ok = false;
    case_loaded = false;
    loaded_case.clear();
    if (!spec_kept) {
        return;
    }
    const std::size_t slash = kept_spec_path.rfind('/');
    const std::string dir = slash == std::string::npos ? "" : kept_spec_path.substr(0, slash + 1);
    std::ifstream in(dir + "case_inputs/" + case_id + ".json");
    if (!in) {
        return;
    }
    const std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    Parser p{text};
    std::map<std::string, CaseEntry> parsed;
    if (!p.eat('{')) {
        return;
    }
    if (!p.eat('}')) {
        do {
            std::string name = p.key();
            CaseEntry entry;
            if (!p.ok || !p.eat(':') || !p.value(entry, 0)) {
                return;
            }
            parsed[name] = std::move(entry);
        } while (p.eat(','));
        if (!p.eat('}')) {
            return;
        }
    }
    p.blanks();
    if (p.at != text.size()) {
        return;
    }
    loaded_case = std::move(parsed);
    case_loaded = true;
    ok = true;
}

dp harness_cpp_gpu__get_real(const std::string& name) {
    return scalar_entry(name).flat.at(0);
}

int harness_cpp_gpu__get_int(const std::string& name) {
    const CaseEntry& e = scalar_entry(name);
    if (!e.integer_token) {
        refuse_read(name, "not an integer token");
    }
    return e.integer_value;
}

bool harness_cpp_gpu__get_bool(const std::string& name) {
    const CaseEntry& e = scalar_entry(name);
    if (!e.bool_token) {
        refuse_read(name, "not the token 1 or 0");
    }
    return e.bool_value;
}

void harness_cpp_gpu__get_array_r1(const std::string& name, std::vector<dp>& a) {
    long extent[1];
    array_entry<1>(name, a, extent);
}

void harness_cpp_gpu__get_array_r2(const std::string& name, atmofab::Array<dp, 2>& a) {
    array_entry<2>(name, a.data, a.extent);
}

void harness_cpp_gpu__get_array_r3(const std::string& name, atmofab::Array<dp, 3>& a) {
    array_entry<3>(name, a.data, a.extent);
}

void harness_cpp_gpu__get_array_r4(const std::string& name, atmofab::Array<dp, 4>& a) {
    array_entry<4>(name, a.data, a.extent);
}

int harness_cpp_gpu__block_size() {
    static const int value = []() {
        const char* env = std::getenv("SPEC2SCIENCE_CUDA_BLOCK");
        if (env == nullptr || env[0] == '\0') {
            return 256;
        }
        const std::string s(env);
        if (s == "32") {
            return 32;
        }
        if (s == "64") {
            return 64;
        }
        if (s == "128") {
            return 128;
        }
        if (s == "256") {
            return 256;
        }
        if (s == "512") {
            return 512;
        }
        if (s == "1024") {
            return 1024;
        }
        std::cerr << "harness_cpp_gpu: SPEC2SCIENCE_CUDA_BLOCK='" << s
                  << "' is not one of 32, 64, 128, 256, 512, 1024\n";
        std::exit(2);
    }();
    return value;
}

}  // namespace harness_cpp_gpu_model
