"""Shared persistence and delivery gating for online-notification identities."""

import threading


class OnlineIdentityService:
    """Manage opaque identities without knowing their device type or origin."""

    def __init__(self, *, state_file, load_set, save_set, send):
        self.state_file = state_file
        self.load_set = load_set
        self.save_set = save_set
        self.send = send
        self._lock = threading.Lock()
        self._inflight = set()

    @staticmethod
    def _clean(identities):
        return {
            str(value).strip()
            for value in identities
            if str(value or "").strip()
        }

    def known_identities(self):
        with self._lock:
            return set(self.load_set(self.state_file))

    def mark_notified(self, *identities):
        clean = self._clean(identities)
        if not clean:
            return False
        with self._lock:
            items = self.load_set(self.state_file)
            if clean.issubset(items):
                return False
            items.update(clean)
            return self.save_set(self.state_file, items)

    def migrate(self, primary, *legacy_identities):
        primary = str(primary or "").strip()
        legacy = self._clean(legacy_identities)
        if not primary:
            return False
        with self._lock:
            items = self.load_set(self.state_file)
            if primary in items:
                return True
            if not (legacy & items):
                return False
            items.add(primary)
            return self.save_set(self.state_file, items)

    def send_once(self, card, *identities):
        clean = self._clean(identities)
        if not clean:
            return False
        # Reserve identities under the lock, but keep delivery network I/O
        # outside it so other watchers can read and update the shared ledger.
        with self._lock:
            items = self.load_set(self.state_file)
            if clean & items:
                if not clean.issubset(items):
                    items.update(clean)
                    self.save_set(self.state_file, items)
                return True
            if clean & self._inflight:
                return False
            self._inflight.update(clean)
        delivered = self.send(card)
        with self._lock:
            self._inflight.difference_update(clean)
            if not delivered:
                return False
            items = self.load_set(self.state_file)
            items.update(clean)
            return self.save_set(self.state_file, items)

    def send_new_lifecycle(self, card, *identities):
        if not self.send(card):
            return False
        self.mark_notified(*identities)
        return True
