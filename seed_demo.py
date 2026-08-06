"""Seed the database with demo data for testing the UI without real WinRM servers."""

import random
import time
from datetime import datetime, timedelta
from database import Database


def seed(hours=24):
    db = Database()
    now = datetime.utcnow()

    servers = [
        ("FileServer-01", "file_server"),
        ("FileServer-02", "file_server"),
        ("FileServer-03", "file_server"),
        ("AppServer-01", "app_server"),
        ("AppServer-02", "app_server"),
        ("AppServer-03", "app_server"),
        ("DC-01", "domain_controller"),
        ("DC-02", "domain_controller"),
    ]

    # Generate 24h of metrics (one per minute)
    points = hours * 60
    print(f"Seeding {points} data points for {len(servers)} servers ({points * len(servers)} total rows)...")

    for server_name, server_type in servers:
        # Each server gets a unique baseline
        cpu_base = random.uniform(10, 40)
        ram_base = random.uniform(40, 70)
        disk_c_base = random.uniform(30, 60)
        disk_d_base = random.uniform(20, 50)

        # One server is "critical" for demo
        if server_name == "FileServer-01":
            disk_c_base = 80  # will trend up to ~95%

        # One server is "warning" for demo
        if server_name == "AppServer-03":
            cpu_base = 65  # will spike occasionally

        for i in range(points):
            ts = now - timedelta(minutes=points - i)
            timestamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Add some noise and trends
            noise = lambda: random.uniform(-5, 5)
            cpu = max(0, min(100, cpu_base + noise() + (10 * (i / points) if server_name == "AppServer-03" else 0)))
            ram = max(0, min(100, ram_base + noise() * 0.5))
            disk_c = max(0, min(100, disk_c_base + (15 * (i / points)) + noise() * 0.3))  # slow upward trend
            disk_d = max(0, min(100, disk_d_base + noise() * 0.3))

            # Determine status
            status = "healthy"
            if disk_c >= 90 or cpu >= 90:
                status = "critical"
            elif disk_c >= 75 or cpu >= 75:
                status = "warning"

            db.insert_metric(server_name, round(cpu, 1), round(ram, 1),
                             round(disk_c, 1), round(disk_d, 1), status, random.randint(50, 300))

    # Seed some events
    db.insert_event("FileServer-01", "warning", "disk_c", 76.5, 75.0, "Disk C: exceeded 75% (76.5%)")
    db.insert_event("FileServer-01", "critical", "disk_c", 91.2, 90.0, "Disk C: exceeded 90% (91.2%)")
    db.insert_event("AppServer-03", "warning", "cpu", 78.3, 70.0, "CPU exceeded 70% (78.3%)")
    db.insert_event("AppServer-02", "warning", "ram", 76.0, 75.0, "RAM exceeded 75% (76.0%)")
    db.insert_event("AppServer-02", "resolved", None, None, None, "Server recovered from warning to healthy")

    print("Done! Demo data seeded.")


if __name__ == "__main__":
    seed()
