"""
🛠️ TOOL UTILS — Ultra-powerful utilities for all actions
Cache, retries, parallelization, error handling, performance optimization
"""

import functools
import json
import logging
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("tool_utils")
logger.setLevel(logging.INFO)

# ────────────────────────────────────────────────────────────────────────────
# 🔄 GLOBAL CACHE (memoization for expensive operations)
# ────────────────────────────────────────────────────────────────────────────

class CacheManager:
    """Thread-safe cache with TTL."""

    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self.ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        """Get cached value if exists and not expired."""
        with self._lock:
            if key in self._cache:
                value, timestamp = self._cache[key]
                if time.time() - timestamp < self.ttl:
                    logger.debug(f"Cache HIT: {key}")
                    return value
                else:
                    del self._cache[key]
            return None

    def set(self, key: str, value: Any) -> None:
        """Set cache value."""
        with self._lock:
            self._cache[key] = (value, time.time())
            logger.debug(f"Cache SET: {key}")

    def clear(self) -> None:
        """Clear all cache."""
        with self._lock:
            self._cache.clear()

GLOBAL_CACHE = CacheManager(ttl_seconds=300)

# ────────────────────────────────────────────────────────────────────────────
# 🔁 RETRY LOGIC (automatic retries for transient failures)
# ────────────────────────────────────────────────────────────────────────────

def retry_on_failure(
    max_retries: int = 3,
    delay_ms: int = 100,
    backoff: float = 1.5,
    on_exception: Optional[Callable[[Exception], bool]] = None
):
    """
    Decorator for automatic retries on failure.

    Args:
        max_retries: Maximum number of attempts
        delay_ms: Initial delay between retries (milliseconds)
        backoff: Exponential backoff multiplier
        on_exception: Optional callback to determine if should retry
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            delay = delay_ms / 1000.0
            last_exception = None

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    should_retry = on_exception is None or on_exception(e)

                    if should_retry and attempt < max_retries - 1:
                        logger.warning(f"Retry {attempt + 1}/{max_retries} for {func.__name__}: {e}")
                        time.sleep(delay)
                        delay *= backoff
                    else:
                        raise

            if last_exception:
                raise last_exception

        return wrapper
    return decorator

# ────────────────────────────────────────────────────────────────────────────
# ⚡ PARALLEL EXECUTION (run multiple tasks simultaneously)
# ────────────────────────────────────────────────────────────────────────────

class ParallelExecutor:
    """Execute multiple tasks in parallel with thread pool."""

    def __init__(self, max_workers: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def run_all(self, tasks: List[Tuple[Callable, Any, Dict[str, Any]]]) -> List[Any]:
        """
        Execute all tasks in parallel.

        Args:
            tasks: List of (function, args, kwargs) tuples

        Returns:
            List of results in same order
        """
        futures = []
        for func, args, kwargs in tasks:
            if not isinstance(args, tuple):
                args = (args,)
            future = self.executor.submit(func, *args, **kwargs)
            futures.append(future)

        results = []
        for future in futures:
            try:
                results.append(future.result(timeout=10))
            except Exception as e:
                logger.error(f"Parallel execution failed: {e}")
                results.append(None)

        return results

    def shutdown(self):
        """Clean up executor."""
        self.executor.shutdown(wait=False)

PARALLEL_EXECUTOR = ParallelExecutor(max_workers=4)

# ────────────────────────────────────────────────────────────────────────────
# 🎯 SMART COMMAND EXECUTION (robust subprocess handling)
# ────────────────────────────────────────────────────────────────────────────

class CommandExecutor:
    """Execute shell commands with robustness."""

    @staticmethod
    def run(
        command: str,
        timeout: int = 10,
        shell: bool = True,
        capture_output: bool = True,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None
    ) -> Tuple[bool, str, str]:
        """
        Execute command robustly.

        Returns:
            (success, stdout, stderr)
        """
        try:
            result = subprocess.run(
                command,
                shell=shell,
                capture_output=capture_output,
                timeout=timeout,
                env=env,
                cwd=cwd,
                text=True
            )
            success = result.returncode == 0
            return success, result.stdout or "", result.stderr or ""
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)

    @staticmethod
    def run_silent(command: str, timeout: int = 5) -> bool:
        """Run command silently, return success status."""
        success, _, _ = CommandExecutor.run(command, timeout=timeout)
        return success

# ────────────────────────────────────────────────────────────────────────────
# 🛡️ ERROR HANDLING (graceful error recovery)
# ────────────────────────────────────────────────────────────────────────────

class ToolError(Exception):
    """Base exception for tool errors."""
    pass

class ToolTimeoutError(ToolError):
    """Tool execution timed out."""
    pass

class ToolUnavailableError(ToolError):
    """Tool or service not available."""
    pass

def handle_tool_error(e: Exception, tool_name: str) -> str:
    """Convert exception to user-friendly message."""
    if isinstance(e, ToolTimeoutError):
        return f"⏱️ {tool_name} a pris trop de temps"
    elif isinstance(e, ToolUnavailableError):
        return f"❌ {tool_name} n'est pas disponible"
    elif isinstance(e, FileNotFoundError):
        return "📁 Fichier ou dossier introuvable"
    elif isinstance(e, PermissionError):
        return "🔒 Permissions insuffisantes"
    else:
        return f"❌ Erreur dans {tool_name}: {str(e)[:100]}"

# ────────────────────────────────────────────────────────────────────────────
# 📊 PERFORMANCE METRICS (measure and log performance)
# ────────────────────────────────────────────────────────────────────────────

class PerformanceTracker:
    """Track execution time of tools."""

    def __init__(self):
        self._times: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def record(self, tool_name: str, duration_ms: float) -> None:
        """Record execution time."""
        with self._lock:
            if tool_name not in self._times:
                self._times[tool_name] = []
            self._times[tool_name].append(duration_ms)

            # Keep only last 100 measurements
            if len(self._times[tool_name]) > 100:
                self._times[tool_name] = self._times[tool_name][-100:]

    def get_stats(self, tool_name: str) -> Dict[str, float]:
        """Get performance statistics."""
        with self._lock:
            times = self._times.get(tool_name, [])
            if not times:
                return {}
            return {
                "avg_ms": sum(times) / len(times),
                "min_ms": min(times),
                "max_ms": max(times),
                "calls": len(times)
            }

PERF_TRACKER = PerformanceTracker()

# ────────────────────────────────────────────────────────────────────────────
# 🎁 DECORATORS (convenient wrappers)
# ────────────────────────────────────────────────────────────────────────────

def cached_tool(ttl_seconds: int = 300):
    """Decorator: cache tool results."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # Create cache key from function name and args
            cache_key = f"{func.__name__}:{str(args)}:{str(sorted(kwargs.items()))}"

            # Try cache first
            cached = GLOBAL_CACHE.get(cache_key)
            if cached is not None:
                return cached

            # Execute function
            result = func(*args, **kwargs)

            # Cache result
            GLOBAL_CACHE.set(cache_key, result)
            return result

        return wrapper
    return decorator

def tracked_tool(func: Callable) -> Callable:
    """Decorator: track performance metrics."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        start = time.time()
        try:
            result = func(*args, **kwargs)
            return result
        finally:
            duration_ms = (time.time() - start) * 1000
            PERF_TRACKER.record(func.__name__, duration_ms)

    return wrapper

# ────────────────────────────────────────────────────────────────────────────
# 📈 UTILITY FUNCTIONS
# ────────────────────────────────────────────────────────────────────────────

def check_command_exists(cmd: str) -> bool:
    """Check if a command is available in PATH.

    ``shutil.which`` lit le PATH en mémoire : pas de shell, pas de processus à
    surveiller, donc rien qui puisse rester bloqué sur le chemin d'un outil.
    """
    return shutil.which(cmd) is not None

def log_tool_call(tool_name: str, parameters: Dict[str, Any], result: str) -> None:
    """Log tool call for debugging."""
    logger.info(f"Tool: {tool_name} | Params: {json.dumps(parameters)[:100]} | Result: {result[:100]}")

# ────────────────────────────────────────────────────────────────────────────
# 🧹 CLEANUP (when tools are done)
# ────────────────────────────────────────────────────────────────────────────

def cleanup_tools():
    """Clean up all tool resources."""
    GLOBAL_CACHE.clear()
    PARALLEL_EXECUTOR.shutdown()
