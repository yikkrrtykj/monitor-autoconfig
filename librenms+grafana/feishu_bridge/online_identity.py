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

    def _can_track(self, identities):
        if len(self._pending | self._inflight | identities) <= self._pending_limit:
            return True
        logging.getLogger(__name__).warning("[ONLINE] pending identity capacity reached; update deferred")
        return False

    def _stage(self, identities):
        if not self._can_track(identities):
            return False
        self._pending.update(identities)
        return True

    def _reserve(self, identities):
        if identities & self._inflight or not self._can_track(identities):
            return False
        self._inflight.update(identities)
        return True

    def _send_reserved(self, card, identities):
        # The reservation keeps space available while network I/O runs unlocked.
        delivered = saved = False
        try:
            delivered = bool(self.send(card))
        finally:
            with self._lock:
                self._inflight.difference_update(identities)
                if delivered:
                    self._pending.update(identities)
                    saved = self._persist(set(self.load_set(self.state_file)) | self._pending)
        return delivered, saved

    def mark_notified(self, *identities):
        clean = self._clean(identities)
        if not clean:
            return False
        with self._lock:
            items = set(self.load_set(self.state_file)) | self._pending
            additions = clean - items
            if not additions and not (clean & self._pending):
                return False
            return self._stage(additions) and self._persist(items | additions)

    def migrate(self, primary, *legacy_identities):
        primary = str(primary or "").strip()
        legacy = self._clean(legacy_identities)
        if not primary:
            return False
        with self._lock:
            items = set(self.load_set(self.state_file)) | self._pending
            if primary in items:
                return primary not in self._pending or self._persist(items)
            if not (legacy & items):
                return False
            additions = {primary}
            return self._stage(additions) and self._persist(items | additions)

    def send_once(self, card, *identities):
        clean = self._clean(identities)
        if not clean:
            return False
        with self._lock:
            items = set(self.load_set(self.state_file)) | self._pending
            if clean & items:
                aliases = clean - items
                if self._pending or aliases:
                    if not self._stage(aliases):
                        return False
                    self._persist(items | aliases)
                return True
            if not self._reserve(clean):
                return False
        _, saved = self._send_reserved(card, clean)
        return saved

    def send_new_lifecycle(self, card, *identities):
        clean = self._clean(identities)
        if not clean:
            return False
        with self._lock:
            if not self._reserve(clean):
                return False
        # An explicit new lifecycle may notify again even with a historical identity.
        delivered, _ = self._send_reserved(card, clean)
        return delivered
