"""SysName change watcher extracted from the Feishu alert bridge.

The bridge continues to own environment parsing, LibreNMS transport, generic
persistence, card presentation, Feishu delivery, health state, and thread
supervision.  This module owns only the SysName polling state machine.
"""

import time


class SysnameChangeWatcher:
    """Poll LibreNMS and notify after a confirmed, meaningful SysName change."""

    def __init__(
        self,
        *,
        enabled,
        librenms_url,
        poll_interval,
        confirm_polls,
        state_file,
        load_state,
        save_state,
        get_token,
        fetch_devices,
        meaningful_sysname,
        sysname_changed,
        build_card,
        send,
        mark_watcher_health,
        log,
        sleep=None,
    ):
        self.enabled = enabled
        self.librenms_url = librenms_url
        self.poll_interval = poll_interval
        self.confirm_polls = confirm_polls
        self.state_file = state_file
        self.load_state = load_state
        self.save_state = save_state
        self.get_token = get_token
        self.fetch_devices = fetch_devices
        self.meaningful_sysname = meaningful_sysname
        self.sysname_changed = sysname_changed
        self.build_card = build_card
        self.send = send
        self.mark_watcher_health = mark_watcher_health
        self.log = log
        self.sleep = sleep or time.sleep

    def run(self):
        if not self.enabled:
            self.log("[SYSNAME] sysName change watcher disabled")
            return
        if not self.librenms_url:
            self.log("[SYSNAME] LIBRENMS_URL not set, sysName change watcher disabled")
            return

        self.sleep(30)
        snapshot = self.load_state(self.state_file)
        pending_changes = {}
        seeded = bool(snapshot)
        self.log(
            "[SYSNAME] sysName change watcher enabled "
            f"(poll={self.poll_interval}s, tracked={len(snapshot)})"
        )

        while True:
            token = self.get_token()
            if not token:
                self.mark_watcher_health(
                    "sysname-change", False, "LibreNMS token unavailable",
                )
                self.log("[SYSNAME] no API token yet, retrying...")
                self.sleep(self.poll_interval)
                continue
            try:
                devices = self.fetch_devices(token)
            except Exception as exc:
                self.mark_watcher_health("sysname-change", False, exc)
                self.log(f"[SYSNAME] poll failed: {exc}")
                self.sleep(self.poll_interval)
                continue
            self.mark_watcher_health("sysname-change", True)

            current = {}
            for dev in devices:
                device_id = str(dev.get("device_id") or "")
                sys_name = str(dev.get("sysName") or "").strip()
                if not device_id:
                    continue
                ip = str(dev.get("ip") or dev.get("hostname") or "").strip()
                hostname = str(dev.get("hostname") or "").strip()
                previous = snapshot.get(device_id) or {}
                prev_name = str(previous.get("sysName") or "").strip()

                # LibreNMS can briefly expose malformed values such as "2"
                # while a poll/update is in flight. Keep a valid baseline so
                # the following good poll cannot manufacture a rename.
                if not self.meaningful_sysname(sys_name):
                    pending_changes.pop(device_id, None)
                    if prev_name:
                        current[device_id] = previous
                    continue

                current[device_id] = {
                    "sysName": sys_name,
                    "ip": ip,
                    "hostname": hostname,
                }
                if seeded and self.sysname_changed(prev_name, sys_name):
                    pending = pending_changes.get(device_id) or {}
                    if str(pending.get("sysName") or "").casefold() == sys_name.casefold():
                        confirmations = int(pending.get("confirmations") or 0) + 1
                    else:
                        confirmations = 1
                    pending_changes[device_id] = {
                        "sysName": sys_name,
                        "confirmations": confirmations,
                    }
                    if confirmations < self.confirm_polls:
                        current[device_id] = previous
                        self.log(
                            f"[SYSNAME] candidate device_id={device_id} "
                            f"{prev_name} -> {sys_name} ({confirmations}/"
                            f"{self.confirm_polls})"
                        )
                        continue
                    self.log(
                        f"[SYSNAME] CHANGE device_id={device_id} "
                        f"{prev_name} -> {sys_name} ({ip})"
                    )
                    if not self.send(self.build_card(
                        prev_name, sys_name, ip=ip, hostname=hostname,
                    )):
                        # Keep the previous persisted baseline so the same
                        # confirmed change is retried on the next good poll.
                        current[device_id] = snapshot[device_id]
                    else:
                        pending_changes.pop(device_id, None)
                else:
                    pending_changes.pop(device_id, None)

            snapshot = current
            self.save_state(self.state_file, snapshot)
            if not seeded:
                seeded = True
                self.log(
                    f"[SYSNAME] baseline recorded for {len(snapshot)} device(s)"
                )

            self.sleep(self.poll_interval)
