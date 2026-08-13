from dataclasses import dataclass

@dataclass
class PingStats:
    sent: int = 0
    received: int = 0

    @property
    def lost(self):
        return self.sent - self.received

    @property
    def loss_rate(self):
        if self.sent == 0:
            return 0
        return self.lost / self.sent * 100
