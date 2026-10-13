// Galaxy AI - C++ Low-Level Engine. Same NDJSON stdio protocol as the
// Rust agents. Zero external dependencies - standard library only.
#include <iostream>
#include <sstream>
#include <string>
#include <vector>
#include <cmath>
#include <algorithm>
#include <cstdint>
#include <optional>
#include <iomanip>

namespace nanojson {

std::optional<std::string> extract_string(const std::string& src, const std::string& key) {
    const std::string pat = "\"" + key + "\"";
    auto key_pos = src.find(pat);
    if (key_pos == std::string::npos) return std::nullopt;
    auto colon = src.find(':', key_pos + pat.size());
    if (colon == std::string::npos) return std::nullopt;
    size_t i = colon + 1;
    while (i < src.size() && std::isspace(static_cast<unsigned char>(src[i]))) i++;
    if (i >= src.size() || src[i] != '"') return std::nullopt;
    i++;
    size_t end = src.find('"', i);
    if (end == std::string::npos) return std::nullopt;
    return src.substr(i, end - i);
}

std::optional<std::vector<double>> extract_array(const std::string& src, const std::string& key) {
    const std::string pat = "\"" + key + "\"";
    auto key_pos = src.find(pat);
    if (key_pos == std::string::npos) return std::nullopt;
    auto colon = src.find(':', key_pos + pat.size());
    if (colon == std::string::npos) return std::nullopt;
    size_t i = colon + 1;
    while (i < src.size() && std::isspace(static_cast<unsigned char>(src[i]))) i++;
    if (i >= src.size() || src[i] != '[') return std::nullopt;
    size_t end = src.find(']', i);
    if (end == std::string::npos) return std::nullopt;
    std::string inner = src.substr(i + 1, end - i - 1);
    std::vector<double> out;
    std::stringstream ss(inner);
    std::string item;
    while (std::getline(ss, item, ',')) {
        try { out.push_back(std::stod(item)); } catch (...) {}
    }
    return out;
}

std::string escape(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        if (c == '\\' || c == '"') out.push_back('\\');
        out.push_back(c);
    }
    return out;
}

}  // namespace nanojson

struct Task {
    std::string id;
    std::string op;
    std::vector<double> payload;
};

std::optional<Task> parse_task(const std::string& line) {
    auto id = nanojson::extract_string(line, "id");
    auto op = nanojson::extract_string(line, "op");
    if (!id || !op) return std::nullopt;
    Task t;
    t.id = *id;
    t.op = *op;
    t.payload = nanojson::extract_array(line, "payload").value_or(std::vector<double>{});
    return t;
}

std::vector<uint64_t> sieve_primes(uint64_t n) {
    if (n < 2) return {};
    std::vector<bool> is_prime(n + 1, true);
    is_prime[0] = is_prime[1] = false;
    for (uint64_t i = 2; i * i <= n; ++i)
        if (is_prime[i])
            for (uint64_t j = i * i; j <= n; j += i) is_prime[j] = false;
    std::vector<uint64_t> out;
    for (uint64_t i = 2; i <= n; ++i) if (is_prime[i]) out.push_back(i);
    return out;
}

// Segmented sieve: given [lo,hi] and the small primes (every prime up to
// sqrt(hi)), crosses off multiples within just this segment - the primitive
// that lets several independent workers sieve disjoint ranges in parallel.
std::vector<uint64_t> segmented_sieve(uint64_t lo, uint64_t hi, const std::vector<uint64_t>& small_primes) {
    if (lo > hi) return {};
    if (lo < 2) lo = 2;
    if (lo > hi) return {};
    uint64_t range_size = hi - lo + 1;
    std::vector<bool> is_prime(range_size, true);
    for (uint64_t p : small_primes) {
        if (p < 2) continue;
        if (p * p > hi) break;
        uint64_t first = std::max(p * p, ((lo + p - 1) / p) * p);
        for (uint64_t j = first; j <= hi; j += p) is_prime[j - lo] = false;
    }
    std::vector<uint64_t> out;
    for (uint64_t i = 0; i < range_size; ++i) if (is_prime[i]) out.push_back(lo + i);
    return out;
}

double matrix_trace(const std::vector<double>& payload) {
    size_t len = payload.size();
    size_t n = static_cast<size_t>(std::sqrt(static_cast<double>(len)));
    if (n * n != len || n == 0) return std::nan("");
    double trace = 0.0;
    for (size_t i = 0; i < n; ++i) trace += payload[i * n + i];
    return trace;
}

struct OpResult {
    bool is_array = false;
    double scalar = 0.0;
    std::vector<double> array;
    std::string to_json_field() const {
        if (is_array) {
            std::ostringstream oss; oss << std::setprecision(17);
            oss << "\"result\": [";
            for (size_t i = 0; i < array.size(); ++i) { if (i) oss << ","; oss << array[i]; }
            oss << "]";
            return oss.str();
        }
        std::ostringstream oss; oss << std::setprecision(17);
        oss << "\"result\": " << scalar;
        return oss.str();
    }
};

std::optional<OpResult> run_op(const Task& task, std::string& err) {
    OpResult r;
    if (task.op == "sum") {
        double s = 0.0;
        for (double v : task.payload) s += v;
        r.scalar = s;
        return r;
    }
    if (task.op == "mean") {
        if (task.payload.empty()) { err = "payload is empty"; return std::nullopt; }
        double s = 0.0;
        for (double v : task.payload) s += v;
        r.scalar = s / static_cast<double>(task.payload.size());
        return r;
    }
    if (task.op == "sort") {
        r.is_array = true;
        r.array = task.payload;
        std::sort(r.array.begin(), r.array.end());
        return r;
    }
    if (task.op == "primes_upto") {
        if (task.payload.empty()) { err = "payload must contain [n]"; return std::nullopt; }
        auto primes = sieve_primes(static_cast<uint64_t>(task.payload[0]));
        r.is_array = true;
        for (auto p : primes) r.array.push_back(static_cast<double>(p));
        return r;
    }
    if (task.op == "segmented_sieve") {
        if (task.payload.size() < 2) { err = "segmented_sieve requires [lo, hi, ...small_primes]"; return std::nullopt; }
        uint64_t lo = static_cast<uint64_t>(task.payload[0]);
        uint64_t hi = static_cast<uint64_t>(task.payload[1]);
        if (lo > hi) { err = "lo must be <= hi"; return std::nullopt; }
        std::vector<uint64_t> small_primes;
        for (size_t i = 2; i < task.payload.size(); ++i) small_primes.push_back(static_cast<uint64_t>(task.payload[i]));
        auto primes = segmented_sieve(lo, hi, small_primes);
        r.is_array = true;
        for (auto p : primes) r.array.push_back(static_cast<double>(p));
        return r;
    }
    if (task.op == "matrix_trace") {
        double t = matrix_trace(task.payload);
        if (std::isnan(t)) { err = "payload length must be a perfect square (NxN flattened)"; return std::nullopt; }
        r.scalar = t;
        return r;
    }
    err = "unknown op '" + task.op + "'";
    return std::nullopt;
}

int main() {
    std::ios::sync_with_stdio(false);
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.find_first_not_of(" \t\r\n") == std::string::npos) continue;
        auto maybe_task = parse_task(line);
        std::string response;
        if (!maybe_task) {
            response = "{\"id\": \"unknown\", \"status\": \"error\", \"error\": \"parse failure\"}";
        } else {
            const Task& task = *maybe_task;
            std::string err;
            auto result = run_op(task, err);
            if (result) {
                std::ostringstream oss; oss << std::setprecision(17);
                oss << "{\"id\": \"" << nanojson::escape(task.id) << "\", \"status\": \"ok\", " << result->to_json_field() << "}";
                response = oss.str();
            } else {
                response = "{\"id\": \"" + nanojson::escape(task.id) + "\", \"status\": \"error\", \"error\": \"" + nanojson::escape(err) + "\"}";
            }
        }
        std::cout << response << "\n" << std::flush;
    }
    return 0;
}
