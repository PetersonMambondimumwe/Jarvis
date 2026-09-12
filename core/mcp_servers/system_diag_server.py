"""
core/mcp_servers/system_diag_server.py
──────────────────────────────────────
Deep Windows System Diagnostics MCP Server for JARVIS.
Zero API keys required. Uses native `psutil`, `platform`, and Windows APIs.

Tools:
  - get_system_overview() -> CPU %, RAM, battery, uptime, OS info
  - get_top_processes(sort_by: str = "memory", limit: int = 5) -> top resource hogs
  - get_disk_usage() -> drive capacities, free space, usage percentages
  - get_network_diagnostics() -> network interfaces, IP addresses, I/O rates
  - find_processes_by_name(name: str) -> search running tasks and resource usage
"""

import asyncio
import datetime
import os
import platform
import socket
import sys
import time
from typing import List, Optional

import psutil
from mcp.server.mcpserver import MCPServer

server = MCPServer("SystemDiagnosticsServer")


def _bytes_to_gb(bytes_val: float) -> float:
    return round(bytes_val / (1024 ** 3), 2)


def _bytes_to_mb(bytes_val: float) -> float:
    return round(bytes_val / (1024 ** 2), 2)


@server.tool()
def get_system_overview() -> str:
    """Get complete real-time system metrics: CPU usage, RAM utilization, battery status, uptime, and hardware specs."""
    try:
        # CPU
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_cores_logical = psutil.cpu_count(logical=True)
        cpu_cores_phys = psutil.cpu_count(logical=False)
        cpu_freq = psutil.cpu_freq()
        freq_str = f"{cpu_freq.current:.0f} MHz" if cpu_freq else "N/A"

        # Memory
        vmem = psutil.virtual_memory()
        ram_total_gb = _bytes_to_gb(vmem.total)
        ram_used_gb = _bytes_to_gb(vmem.used)
        ram_avail_gb = _bytes_to_gb(vmem.available)
        ram_percent = vmem.percent

        # Swap
        swap = psutil.swap_memory()
        swap_used_gb = _bytes_to_gb(swap.used)
        swap_total_gb = _bytes_to_gb(swap.total)

        # Uptime
        boot_timestamp = psutil.boot_time()
        boot_time = datetime.datetime.fromtimestamp(boot_timestamp)
        uptime_seconds = int(time.time() - boot_timestamp)
        uptime_str = str(datetime.timedelta(seconds=uptime_seconds))

        # Battery
        battery = psutil.sensors_battery()
        if battery:
            plugged = "Plugged In (Charging)" if battery.power_plugged else "On Battery"
            battery_str = f"{battery.percent}% [{plugged}]"
            secs = getattr(battery, "secsleft", None)
            power_unlimited = getattr(psutil, "POWER_TIME_UNLIMITED", -1)
            if secs is not None and secs != power_unlimited and secs > 0:
                time_left = str(datetime.timedelta(seconds=secs))
                battery_str += f" ({time_left} remaining)"
        else:
            battery_str = "Desktop / No battery detected"

        # OS Details
        os_info = f"{platform.system()} {platform.release()} (Build {platform.version()})"
        hostname = socket.gethostname()

        return (
            f"=== System Hardware & Performance Overview ===\n"
            f"- Machine: {hostname} ({platform.machine()})\n"
            f"- OS: {os_info}\n"
            f"- Uptime: {uptime_str} (Booted at {boot_time.strftime('%Y-%m-%d %H:%M')})\n"
            f"- CPU: {cpu_percent}% utilized ({cpu_cores_phys} cores, {cpu_cores_logical} threads @ {freq_str})\n"
            f"- RAM: {ram_used_gb} GB / {ram_total_gb} GB ({ram_percent}% used, {ram_avail_gb} GB free)\n"
            f"- Swap: {swap_used_gb} GB / {swap_total_gb} GB ({swap.percent}% used)\n"
            f"- Power: {battery_str}"
        )
    except Exception as e:
        return f"Error retrieving system overview: {e}"


@server.tool()
def get_top_processes(sort_by: str = "memory", limit: int = 5) -> str:
    """List the top processes consuming the most system resources.

    Parameters:
      - sort_by: 'memory' (default) or 'cpu'
      - limit: number of processes to return (default: 5, max: 20)
    """
    try:
        limit = max(1, min(limit, 20))
        sort_by = sort_by.lower().strip()
        if sort_by not in ("memory", "cpu"):
            sort_by = "memory"

        processes = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "status"]):
            try:
                info = p.info
                mem_mb = _bytes_to_mb(info["memory_info"].rss) if info.get("memory_info") else 0.0
                processes.append({
                    "pid": info["pid"],
                    "name": info["name"] or "Unknown",
                    "mem_mb": mem_mb,
                    "cpu_percent": info.get("cpu_percent") or 0.0,
                    "status": info.get("status", "running"),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        if sort_by == "cpu":
            processes.sort(key=lambda x: x["cpu_percent"], reverse=True)
            sort_label = "CPU %"
        else:
            processes.sort(key=lambda x: x["mem_mb"], reverse=True)
            sort_label = "Memory Usage"

        lines = [f"=== Top {limit} Processes by {sort_label} ===\n"]
        for idx, proc in enumerate(processes[:limit], 1):
            lines.append(
                f"{idx}. {proc['name']} (PID {proc['pid']})\n"
                f"   - RAM: {proc['mem_mb']} MB\n"
                f"   - CPU: {proc['cpu_percent']}%\n"
                f"   - Status: {proc['status']}"
            )

        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving top processes: {e}"


@server.tool()
def get_disk_usage() -> str:
    """Inspect all storage drives, total capacities, used space, free space, and filesystem formats."""
    try:
        partitions = psutil.disk_partitions(all=False)
        lines = ["=== Disk Storage Diagnostic ===\n"]

        for part in partitions:
            try:
                usage = psutil.disk_usage(part.mountpoint)
                total_gb = _bytes_to_gb(usage.total)
                used_gb = _bytes_to_gb(usage.used)
                free_gb = _bytes_to_gb(usage.free)
                percent = usage.percent

                lines.append(
                    f"- Drive {part.device} (Mount: {part.mountpoint}, Type: {part.fstype}):\n"
                    f"  Total: {total_gb} GB | Used: {used_gb} GB ({percent}%) | Free: {free_gb} GB"
                )
            except PermissionError:
                continue

        # Disk I/O Counters
        io_counters = psutil.disk_io_counters()
        if io_counters:
            read_gb = _bytes_to_gb(io_counters.read_bytes)
            write_gb = _bytes_to_gb(io_counters.write_bytes)
            lines.append(f"\n- Disk I/O Cumulative: Read {read_gb} GB | Written {write_gb} GB")

        return "\n".join(lines)
    except Exception as e:
        return f"Error checking disk usage: {e}"


@server.tool()
def get_network_diagnostics() -> str:
    """Check active network adapters, local IP addresses, network traffic counters, and connection health."""
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        io_counters = psutil.net_io_counters(pernic=False)

        lines = ["=== Network Adapters & Traffic Diagnostic ===\n"]

        for iface_name, addr_list in addrs.items():
            stat = stats.get(iface_name)
            is_up = "UP" if (stat and stat.isup) else "DOWN"
            speed = f"{stat.speed} Mbps" if (stat and stat.speed > 0) else "N/A"

            ip_v4 = "N/A"
            for a in addr_list:
                if a.family == socket.AF_INET:
                    ip_v4 = a.address

            if ip_v4 != "N/A" or is_up == "UP":
                lines.append(
                    f"- Adapter '{iface_name}' [{is_up}]:\n"
                    f"  IPv4: {ip_v4} | Speed: {speed}"
                )

        if io_counters:
            sent_mb = _bytes_to_mb(io_counters.bytes_sent)
            recv_mb = _bytes_to_mb(io_counters.bytes_recv)
            lines.append(f"\n- Session Network I/O: Sent {sent_mb} MB | Received {recv_mb} MB")

        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving network diagnostics: {e}"


@server.tool()
def find_processes_by_name(name: str) -> str:
    """Search for all running processes matching a given application name (e.g. 'chrome', 'python', 'code', 'node')."""
    try:
        needle = name.lower().strip()
        matches = []

        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "status", "create_time"]):
            try:
                pname = p.info.get("name") or ""
                if needle in pname.lower():
                    mem_mb = _bytes_to_mb(p.info["memory_info"].rss) if p.info.get("memory_info") else 0.0
                    matches.append({
                        "pid": p.info["pid"],
                        "name": pname,
                        "mem_mb": mem_mb,
                        "cpu_percent": p.info.get("cpu_percent") or 0.0,
                        "status": p.info.get("status", "running"),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not matches:
            return f"No active processes found matching '{name}'."

        lines = [f"Found {len(matches)} process(es) matching '{name}':\n"]
        total_mem = sum(m["mem_mb"] for m in matches)
        for m in matches[:15]:
            lines.append(f"- {m['name']} (PID {m['pid']}): {m['mem_mb']} MB RAM | CPU: {m['cpu_percent']}% | Status: {m['status']}")

        lines.append(f"\nTotal Memory Consumed by '{name}': {round(total_mem, 2)} MB")
        return "\n".join(lines)
    except Exception as e:
        return f"Error finding processes for '{name}': {e}"


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
