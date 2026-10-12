"""Shared persistence and delivery gating for online-notification identities."""

import logging
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
        self._pending = set()
        self._pending_limit = 4096

    @staticmethod
    def _clean(identities):
        return {
            str(value).strip()
            for value in identities
            if str(value or "").strip()
        }

    def known_identities(self):
        with self._lock:
            items = set(self.load_set(self.state_file))
            if self._pending:
                items.update(self._pending)
                self._persist(items)
            return items

    def _persist(self, items):
        """Called under the lock; delivery success survives a failed local save."""
        try:
            saved = self.save_set(self.state_file, items)
        except OSError:
            saved = False
        if saved:
            self._pending.difference_update(items)
        else:
            logging.getLogger(__name__).warning(
                "[ONLINE] notification identity persistence failed; pending=%d; "
                "restart before persistence may duplicate delivery", len(self._pending)
            )
        return saved

    def mark_notified(self, *identities):
        clean = self._clean(identities)
        if not clean:
            return False
        with self._lock:
            items = set(self.load_set(self.state_file)) | self._pending
            if clean.issubset(items):
                return False
            items.update(clean)
            return self._persist(items)

    def migrate(self, primary, *legacy_identities):
        primary = str(primary or "").strip()
        legacy = self._clean(legacy_identities)
        if not primary:
            return False
        with self._lock:
            items = set(self.load_set(self.state_file)) | self._pending
            if primary in items:
                return True
            if not (legacy & items):
                return False
            items.add(primary)
            return self._persist(items)

    def send_once(self, card, *identities):
        clean = self._clean(identities)
        if not clean:
            return False
        # Reserve identities under the lock, but keep delivery network I/O
        # outside it so other watchers can read and update the shared ledger.
        with self._lock:
            items = set(self.load_set(self.state_file)) | self._pending
            if clean & items:
                if self._pending or not clean.issubset(items):
                    items.update(clean)
                    self._persist(items)
                return True
            if clean & self._inflight:
                return False
            if len(self._pending | self._inflight | clean) > self._pending_limit:
                logging.getLogger(__name__).warning("[ONLINE] pending identity capacity reached; delivery deferred")
                return False
            self._inflight.update(clean)
        try:
            delivered = self.send(card)
        except Exception:
            with self._lock:
                self._inflight.difference_update(clean)
            raise
        with self._lock:
            self._inflight.difference_update(clean)
            if not delivered:
                return False
            items = set(self.load_set(self.state_file)) | self._pending
            self._pending.update(clean)
            items.update(clean)
            return self._persist(items)

    def send_new_lifecycle(self, card, *identities):
        if not self.send(card):
            return False
        self.mark_notified(*identities)
        return True
