"""A return must follow an observed absence and a sustained connection."""


class Arrival:
    def __init__(self):
        self.missed = 0
        self.armed = False
        self.since = None
        self.retry_at = 0

    def disconnected(self):
        self.missed += 1
        if self.missed >= 2:
            self.armed = True
        self.since = None

    def ready(self, now, home_and_parked):
        self.missed = 0
        if not home_and_parked:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return self.armed and now - self.since >= 300 and now >= self.retry_at

    def sent(self):
        self.armed = False

    def retry(self, now):
        self.retry_at = now + 600
