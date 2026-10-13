// Galaxy AI - Rust Compute Engine. Reads one JSON object per line on stdin,
// writes one JSON object per line on stdout. Zero external crates so
// `cargo build --release` needs nothing from crates.io.
use std::io::{self, BufRead, Write};

struct Task {
    id: String,
    op: String,
    payload: Vec<f64>,
}

mod nanojson {
    pub fn extract_string(src: &str, key: &str) -> Option<String> {
        let pat = format!("\"{}\"", key);
        let key_pos = src.find(&pat)?;
        let after_key = &src[key_pos + pat.len()..];
        let colon = after_key.find(':')?;
        let after_colon = after_key[colon + 1..].trim_start();
        if !after_colon.starts_with('"') {
            return None;
        }
        let rest = &after_colon[1..];
        let end = rest.find('"')?;
        Some(rest[..end].to_string())
    }

    pub fn extract_array(src: &str, key: &str) -> Option<Vec<f64>> {
        let pat = format!("\"{}\"", key);
        let key_pos = src.find(&pat)?;
        let after_key = &src[key_pos + pat.len()..];
        let colon = after_key.find(':')?;
        let after_colon = after_key[colon + 1..].trim_start();
        if !after_colon.starts_with('[') {
            return None;
        }
        let end = after_colon.find(']')?;
        let inner = &after_colon[1..end];
        Some(
            inner
                .split(',')
                .filter_map(|s| s.trim().parse::<f64>().ok())
                .collect(),
        )
    }
}

fn parse_task(line: &str) -> Result<Task, String> {
    let id = nanojson::extract_string(line, "id").ok_or("missing 'id'")?;
    let op = nanojson::extract_string(line, "op").ok_or("missing 'op'")?;
    let payload = nanojson::extract_array(line, "payload").unwrap_or_default();
    Ok(Task { id, op, payload })
}

fn escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

enum OpResult {
    Number(f64),
    Array(Vec<f64>),
}

impl OpResult {
    fn to_json_field(&self) -> String {
        match self {
            OpResult::Number(n) => format!("\"result\": {}", n),
            OpResult::Array(arr) => {
                let items: Vec<String> = arr.iter().map(|v| v.to_string()).collect();
                format!("\"result\": [{}]", items.join(","))
            }
        }
    }
}

fn sieve_primes(n: usize) -> Vec<usize> {
    if n < 2 {
        return vec![];
    }
    let mut is_prime = vec![true; n + 1];
    is_prime[0] = false;
    is_prime[1] = false;
    let mut i = 2;
    while i * i <= n {
        if is_prime[i] {
            let mut j = i * i;
            while j <= n {
                is_prime[j] = false;
                j += i;
            }
        }
        i += 1;
    }
    (2..=n).filter(|&i| is_prime[i]).collect()
}

fn run_op(task: &Task) -> Result<OpResult, String> {
    match task.op.as_str() {
        "sum" => Ok(OpResult::Number(task.payload.iter().sum())),
        "mean" => {
            if task.payload.is_empty() {
                return Err("payload is empty, cannot compute mean".into());
            }
            Ok(OpResult::Number(
                task.payload.iter().sum::<f64>() / task.payload.len() as f64,
            ))
        }
        "stddev" => {
            let n = task.payload.len();
            if n == 0 {
                return Err("payload is empty, cannot compute stddev".into());
            }
            let mean = task.payload.iter().sum::<f64>() / n as f64;
            let variance =
                task.payload.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / n as f64;
            Ok(OpResult::Number(variance.sqrt()))
        }
        "sort" => {
            let mut v = task.payload.clone();
            v.sort_by(|a, b| a.partial_cmp(b).unwrap());
            Ok(OpResult::Array(v))
        }
        "primes_upto" => {
            let n = *task.payload.get(0).unwrap_or(&0.0) as usize;
            Ok(OpResult::Array(
                sieve_primes(n).into_iter().map(|p| p as f64).collect(),
            ))
        }
        other => Err(format!("unknown op '{}'", other)),
    }
}

fn main() {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = stdout.lock();

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) if !l.trim().is_empty() => l,
            Ok(_) => continue,
            Err(_) => break,
        };

        let response = match parse_task(&line) {
            Ok(task) => {
                let task_id = task.id.clone();
                match run_op(&task) {
                    Ok(result) => format!(
                        "{{\"id\": \"{}\", \"status\": \"ok\", {}}}",
                        escape(&task_id),
                        result.to_json_field()
                    ),
                    Err(e) => format!(
                        "{{\"id\": \"{}\", \"status\": \"error\", \"error\": \"{}\"}}",
                        escape(&task_id),
                        escape(&e)
                    ),
                }
            }
            Err(e) => format!(
                "{{\"id\": \"unknown\", \"status\": \"error\", \"error\": \"parse failure: {}\"}}",
                escape(&e)
            ),
        };

        writeln!(out, "{}", response).ok();
        out.flush().ok();
    }
}
