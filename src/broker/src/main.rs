//! pai-broker — the surviving privileged code (FILESYSTEM_v4.md §4).
//!
//! Runs as its own system uid with group `adm`; members cannot bypass what
//! they cannot reach. At v4.0 the broker is dormant but resident: it owns
//! the audit log, loads the capability policy, and answers the fleet
//! socket. The egress path (outbox spool → policy → console modal → send →
//! audit) lands with the first integration; its design is fixed, its code
//! is not yet here.
//!
//! Deliberately std-only: no async runtime, no YAML parser (nothing to
//! enforce yet), blocking accept loop, one thread per connection. The
//! socket is the trust boundary — /run/pai/broker.sock, group adm, 0660.

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{chown, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::process::Command;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

const SOCK: &str = "/run/pai/broker.sock";
const AUDIT: &str = "/var/log/pai/audit.log";
const CONFIG: &str = "/etc/pai/config.yaml";
const SPOOL: &str = "/var/spool/pai";

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// ISO-8601 UTC without a date crate (civil_from_days, Hinnant).
fn iso_utc(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let (h, m, s) = (secs % 86_400 / 3600, secs % 3600 / 60, secs % 60);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{d:02}T{h:02}:{m:02}:{s:02}Z")
}

/// Append-only audit line: ts \t actor \t action \t detail.
fn audit(actor: &str, action: &str, detail: &str) {
    let line = format!("{}\t{actor}\t{action}\t{detail}\n", iso_utc(now_secs()));
    match OpenOptions::new().create(true).append(true).open(AUDIT) {
        Ok(mut f) => {
            let _ = f.write_all(line.as_bytes());
        }
        Err(e) => eprintln!("pai-broker: audit append failed: {e}"),
    }
}

fn adm_gid() -> Option<u32> {
    let groups = fs::read_to_string("/etc/group").ok()?;
    groups.lines().find_map(|l| {
        let mut fields = l.split(':');
        if fields.next()? != "adm" {
            return None;
        }
        fields.next()?; // password slot
        fields.next()?.parse().ok()
    })
}

/// Fleet view: one line per member — name, unit state, unread count.
/// Members are whoever has a spool; unit state comes from systemd itself.
fn fleet() -> String {
    let mut names: Vec<String> = match fs::read_dir(SPOOL) {
        Ok(rd) => rd
            .flatten()
            .filter(|e| e.path().is_dir())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect(),
        Err(_) => Vec::new(),
    };
    if names.is_empty() {
        return "(no members)\n".to_string();
    }
    names.sort();
    let mut out = String::new();
    for name in names {
        let active = Command::new("systemctl")
            .args(["is-active", "--quiet", &format!("pai@{name}")])
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        // The inbox is a no-list drop box (mode 3730); the broker may
        // legitimately be unable to count it.
        let unread = fs::read_dir(Path::new(SPOOL).join(&name).join("in"))
            .map(|d| d.flatten().count().to_string())
            .unwrap_or_else(|_| "-".to_string());
        out.push_str(&format!(
            "{name}\t{}\t{unread}\n",
            if active { "active" } else { "inactive" }
        ));
    }
    out
}

fn tail_audit(n: usize) -> String {
    match fs::read_to_string(AUDIT) {
        Ok(text) => {
            let lines: Vec<&str> = text.lines().collect();
            let start = lines.len().saturating_sub(n);
            let mut out = lines[start..].join("\n");
            out.push('\n');
            out
        }
        Err(_) => "(no audit log)\n".to_string(),
    }
}

fn handle(stream: UnixStream) {
    let mut reader = BufReader::new(match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    });
    let mut stream = stream;
    let mut line = String::new();
    if reader.read_line(&mut line).is_err() {
        return;
    }
    let cmd = line.trim();
    let response = match cmd {
        "ping" => "pong\n".to_string(),
        "fleet" => fleet(),
        "audit" => tail_audit(50),
        _ => "unknown command (ping | fleet | audit)\n".to_string(),
    };
    let _ = stream.write_all(response.as_bytes());
}

fn main() {
    // Policy is loaded for the audit trail; there is nothing to enforce
    // until the egress path ships with the first integration.
    match fs::metadata(CONFIG) {
        Ok(meta) => audit(
            "broker",
            "policy_loaded",
            &format!("{CONFIG} ({} bytes)", meta.len()),
        ),
        Err(_) => audit("broker", "policy_missing", CONFIG),
    }

    let _ = fs::remove_file(SOCK);
    let listener = match UnixListener::bind(SOCK) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("pai-broker: cannot bind {SOCK}: {e}");
            std::process::exit(1);
        }
    };
    if let Some(gid) = adm_gid() {
        let _ = chown(SOCK, None, Some(gid));
    }
    let _ = fs::set_permissions(SOCK, fs::Permissions::from_mode(0o660));
    audit("broker", "start", env!("CARGO_PKG_VERSION"));
    println!("pai-broker: listening on {SOCK}");

    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                thread::spawn(move || handle(s));
            }
            Err(e) => eprintln!("pai-broker: accept failed: {e}"),
        }
    }
}
