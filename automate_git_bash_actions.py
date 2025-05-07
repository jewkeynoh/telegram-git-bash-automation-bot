# automate_git_bash_actions.py
"""
Automates Git Bash interactions using pywinauto for GUI control.
Manages a persistent Git Bash session: launching, connecting, command execution, and closure.
NOTE: UI automation is inherently brittle. This module attempts to be robust but may require
tuning of timeouts and selectors based on the target system's performance and Git Bash version/hosting.
A class-based approach for session management could be a future enhancement for larger-scale projects.
"""

import logging
import subprocess
import time
from pathlib import Path
import requests
from typing import Optional, List
import uuid

from pywinauto.application import Application, ProcessNotFoundError
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PywinautoTimeoutError, wait_until_passes
from pywinauto import Desktop

logger = logging.getLogger(__name__)

_persistent_git_bash_process: Optional[subprocess.Popen] = None
_persistent_git_bash_app: Optional[Application] = None
_persistent_git_bash_dlg = None

def translate_path_to_bash(windows_path: str) -> str:
    """Converts Windows paths (e.g., C:\\Users) to Bash paths (/c/Users)."""
    if not windows_path or ':' not in windows_path:
        return windows_path
    try:
        path_obj = Path(windows_path)
        drive = path_obj.drive.replace(':', '').lower()
        rest_of_path = "/".join(path_obj.parts[1:])
        bash_path = f"/{drive}/{rest_of_path}"
        logger.debug(f"Translated '{windows_path}' to Bash path '{bash_path}'")
        return bash_path
    except Exception as e:
        logger.error(f"Path translation error for '{windows_path}': {e}", exc_info=True)
        return windows_path

def _launch_and_connect_new_bash_session(gb_cfg: dict):
    """Launches and connects to a new Git Bash session, updating global session variables."""
    global _persistent_git_bash_process, _persistent_git_bash_dlg, _persistent_git_bash_app

    if _persistent_git_bash_process and _persistent_git_bash_process.poll() is None:
        logger.warning(f"Terminating existing Git Bash (PID: {_persistent_git_bash_process.pid}) before new launch.")
        close_persistent_git_bash_session(called_internally=True)

    _persistent_git_bash_process, _persistent_git_bash_dlg, _persistent_git_bash_app = None, None, None

    exe_path = gb_cfg['executable_path']
    py_cfg = gb_cfg
    launch_wait = py_cfg.get('window_launch_wait_seconds', 10.0)
    connect_timeout = py_cfg.get('window_connect_timeout_seconds', 30)
    backend = py_cfg.get('pywinauto_backend', 'win32')
    win_class = py_cfg.get('window_class_name', 'CASCADIA_HOSTING_WINDOW_CLASS')
    win_title_re = py_cfg.get('window_title_regex', '.*MINGW64.*')
    elem_ready_timeout = py_cfg.get('window_element_ready_timeout_seconds', 10)


    logger.info(f"Launching Git Bash: {exe_path} (Backend: {backend})")
    try:
        _persistent_git_bash_process = subprocess.Popen([str(exe_path)])
        logger.info(f"Git Bash process started (PID: {_persistent_git_bash_process.pid}). Waiting {launch_wait}s for window.")
        time.sleep(launch_wait)
    except Exception as e:
        logger.critical(f"Failed to launch Git Bash '{exe_path}': {e}", exc_info=True)
        return None

    logger.info(f"Finding Git Bash window (PID:{_persistent_git_bash_process.pid}). Class:'{win_class}', TitleRE:'{win_title_re}'.")
    try:
        def find_window_by_pid_criteria():
            window = Desktop(backend=backend).window(
                class_name=win_class, title_re=win_title_re, process=_persistent_git_bash_process.pid,
                visible_only=True, top_level_only=True
            )
            if not window.exists(): raise ElementNotFoundError("Window not found by PID criteria yet.")
            return window
        
        temp_dlg = wait_until_passes(connect_timeout, 1, find_window_by_pid_criteria)
        _persistent_git_bash_app = Application(backend=backend).connect(handle=temp_dlg.handle)
        _persistent_git_bash_dlg = _persistent_git_bash_app.window(handle=temp_dlg.handle)
        logger.info(f"Connected to Git Bash window (PID:{_persistent_git_bash_process.pid}): '{_persistent_git_bash_dlg.window_text()}'")
        _persistent_git_bash_dlg.wait('ready visible enabled', timeout=elem_ready_timeout, retry_interval=0.5)
        return _persistent_git_bash_dlg
    except (ElementNotFoundError, PywinautoTimeoutError, ProcessNotFoundError, RuntimeError) as e:
        logger.warning(f"PID-specific window search failed for PID {_persistent_git_bash_process.pid}: {e}. Trying general search.")
    except Exception as e_pid_search:
        logger.error(f"Unexpected error in PID-specific search: {e_pid_search}", exc_info=True)

    try:
        logger.warning("Attempting general window search by class and title regex...")
        possible_dialogs = Desktop(backend=backend).windows(class_name=win_class, title_re=win_title_re, visible_only=True, top_level_only=True)
        if not possible_dialogs: logger.error("General search: No matching windows found.")
        elif len(possible_dialogs) == 1:
            temp_dlg_fb = possible_dialogs[0]
            _persistent_git_bash_app = Application(backend=backend).connect(handle=temp_dlg_fb.handle)
            _persistent_git_bash_dlg = _persistent_git_bash_app.window(handle=temp_dlg_fb.handle)
            found_pid = _persistent_git_bash_dlg.process_id()
            logger.info(f"General search: Found unique window: '{_persistent_git_bash_dlg.window_text()}' (PID from window: {found_pid})")
            if found_pid != _persistent_git_bash_process.pid:
                logger.warning(f"Found window PID ({found_pid}) differs from launched PID ({_persistent_git_bash_process.pid}). Potential mismatch.")
            _persistent_git_bash_dlg.wait('ready visible enabled', timeout=elem_ready_timeout, retry_interval=0.5)
            return _persistent_git_bash_dlg
        else:
            logger.error(f"General search: Ambiguous. Found {len(possible_dialogs)} windows. Cannot select. PIDs: {[d.process_id() for d in possible_dialogs]}")
    except Exception as e_fallback:
        logger.error(f"Error during fallback general search: {e_fallback}", exc_info=True)

    if _persistent_git_bash_process and _persistent_git_bash_process.poll() is None:
        logger.warning(f"Failed to connect to window for launched Git Bash (PID:{_persistent_git_bash_process.pid}). Terminating.")
        _persistent_git_bash_process.terminate()
        try: _persistent_git_bash_process.wait(timeout=2)
        except subprocess.TimeoutExpired: _persistent_git_bash_process.kill()
    _persistent_git_bash_process = None
    return None

def ensure_git_bash_session(gb_cfg: dict):
    """Ensures a valid Git Bash session is active, reusing or launching as needed."""
    global _persistent_git_bash_process, _persistent_git_bash_dlg
    
    py_cfg = gb_cfg 
    elem_ready_timeout = py_cfg.get('window_element_ready_timeout_seconds', 10)


    is_ok = False
    if _persistent_git_bash_process and _persistent_git_bash_dlg and _persistent_git_bash_process.poll() is None:
        try:
            if _persistent_git_bash_dlg.exists() and _persistent_git_bash_dlg.is_visible() and _persistent_git_bash_dlg.is_enabled():
                logger.info(f"Reusing active Git Bash session (PID: {_persistent_git_bash_process.pid}).")
                if not _persistent_git_bash_dlg.is_active(): _persistent_git_bash_dlg.set_focus()
                time.sleep(0.2) # Settle focus
                is_ok = True
            else: logger.warning("Existing Git Bash window no longer valid. Will relaunch.")
        except Exception as e_check: logger.warning(f"Error checking existing session: {e_check}. Will relaunch.")
    else: logger.info(f"No active session or process terminated (PID: {_persistent_git_bash_process.pid if _persistent_git_bash_process else 'N/A'}). Will launch new.")

    if not is_ok:
        logger.info("Launching new Git Bash session...")
        new_dlg = _launch_and_connect_new_bash_session(gb_cfg)
        if new_dlg:
            _persistent_git_bash_dlg = new_dlg
            logger.info("New Git Bash session established.")

            session_default_dir = gb_cfg.get('default_target_directory_windows')
            cmd_pause = gb_cfg.get('command_pause_seconds', 3.0)
            if session_default_dir:
                cd_cmd = f'cd "{translate_path_to_bash(str(session_default_dir))}"'
                logger.info(f"New session: Initial CD to session default: {cd_cmd}")
                try:
                    if not _persistent_git_bash_dlg.is_active(): _persistent_git_bash_dlg.set_focus()
                    time.sleep(0.5)
                    _persistent_git_bash_dlg.type_keys(cd_cmd + "{ENTER}", with_spaces=True, pause=0.05)
                    time.sleep(cmd_pause)
                except Exception as e_init_cd:
                    logger.error(f"Failed initial CD in new session ('{cd_cmd}'): {e_init_cd}. Session CWD may be unexpected.", exc_info=True)
            return _persistent_git_bash_dlg
        else:
            logger.error("Failed to launch/connect new bash session in ensure_git_bash_session.")
            return None
    return _persistent_git_bash_dlg

def execute_git_bash_commands(gb_cfg: dict, commands: List[str]) -> dict:
    """Executes commands in the persistent Git Bash session."""
    action_details = []
    command_outputs = [] 
    
    active_dlg = ensure_git_bash_session(gb_cfg)
    if not active_dlg:
        msg = "Failed to establish Git Bash session."
        logger.error(msg)
        return {"success": False, "message": msg, "details": [msg], "error_details": "No valid Git Bash window."}

    task_dir_win_from_cfg = gb_cfg.get('target_directory_windows')
    task_dir_win_str = str(task_dir_win_from_cfg) if task_dir_win_from_cfg else None

    cmd_pause = gb_cfg.get('command_pause_seconds', 3.0)
    
    base_temp_dir = Path(task_dir_win_str if task_dir_win_str else Path.home() / "temp_git_bash_output")
    base_temp_dir.mkdir(parents=True, exist_ok=True)
    temp_output_filename = f"git_bash_out_{uuid.uuid4().hex[:8]}.txt"
    temp_output_file_windows_path = base_temp_dir / temp_output_filename
    temp_output_file_bash_path = translate_path_to_bash(str(temp_output_file_windows_path))
    logger.debug(f"Using temp file for output: Windows='{temp_output_file_windows_path}', Bash='{temp_output_file_bash_path}'")

    try:
        logger.info(f"Interacting with Git Bash: {active_dlg.window_text()}")
        action_details.append(f"Using window: {active_dlg.window_text()}")

        if not active_dlg.is_active(): active_dlg.set_focus(); time.sleep(1.0)
        if not active_dlg.is_active(): action_details.append("WARN: Window focus issue."); logger.warning("Window focus issue after set_focus().")

        if task_dir_win_str:
            cd_cmd = f'cd "{translate_path_to_bash(task_dir_win_str)}"'
            logger.info(f"Task CD: {cd_cmd} (Target directory was specified)")
            action_details.append(f"Task CD: {cd_cmd}")
            active_dlg.type_keys(cd_cmd + "{ENTER}", with_spaces=True, pause=0.05)
            time.sleep(cmd_pause)
        else:
            action_details.append("No task-specific directory provided; commands will run in the current Bash CWD without an initial CD.")
            logger.info("No task-specific directory provided; using current Git Bash CWD for commands.")

        if not commands:
            action_details.append("No commands for this task.")
        else:
            for i, cmd_str in enumerate(commands):
                logger.info(f"Preparing cmd {i+1}/{len(commands)}: {cmd_str}")
                action_details.append(f"Preparing: {cmd_str}")
                
                cmd_with_redirect = f'{cmd_str.strip()} > "{temp_output_file_bash_path}" 2>&1'
                cmd_type = cmd_with_redirect + "{ENTER}"
                
                logger.info(f"Typing redirected cmd: {cmd_with_redirect}")
                if not active_dlg.is_active(): active_dlg.set_focus(); time.sleep(0.2)
                active_dlg.type_keys(cmd_type, with_spaces=True, pause=0.05)
                time.sleep(cmd_pause)

                output_content = ""
                if not (_persistent_git_bash_process and _persistent_git_bash_process.poll() is None and 
                        active_dlg and active_dlg.exists() and active_dlg.is_enabled()):
                    logger.error(f"Git Bash session became invalid after attempting to execute: {cmd_str}")
                    close_persistent_git_bash_session(called_internally=True) 
                    output_content = "[Git Bash session became invalid or closed during command execution]"
                elif temp_output_file_windows_path.exists():
                    try:
                        output_content = temp_output_file_windows_path.read_text(encoding='utf-8', errors='replace')
                        logger.debug(f"Output for '{cmd_str}':\n{output_content[:500]}...")
                    except Exception as e_read:
                        logger.error(f"Failed to read output file '{temp_output_file_windows_path}': {e_read}")
                        output_content = f"[Error reading output file: {e_read}]"
                else:
                    logger.warning(f"Output file '{temp_output_file_windows_path}' not found after command: {cmd_str}")
                    output_content = "[Output file not found, command may not have produced output or redirected correctly]"
                
                command_outputs.append(f"--- Output for: {cmd_str} ---\n{output_content.strip()}")

        if temp_output_file_windows_path.exists():
            try: temp_output_file_windows_path.unlink()
            except Exception as e_del: logger.warning(f"Could not delete temp output file '{temp_output_file_windows_path}': {e_del}")

        action_details.append("Finished processing commands.")
        return {"success": True, "message": "Commands processed.", "details": command_outputs, "action_log": action_details}

    except Exception as e:
        err_msg = f"Error interacting with Git Bash: {e}"
        logger.error(err_msg, exc_info=True)
        action_details.append(err_msg)
        if temp_output_file_windows_path.exists(): 
            try: temp_output_file_windows_path.unlink(missing_ok=True)
            except Exception as e_del_err: logger.warning(f"Error deleting temp file during exception: {e_del_err}")
        close_persistent_git_bash_session(called_internally=True)
        return {"success": False, "message": "Error interacting with Git Bash.", "details": command_outputs, "action_log": action_details, "error_details": str(e)}


def close_persistent_git_bash_session(
    bot_token_for_notification: Optional[str] = None,
    chat_id_for_notification: Optional[int] = None,
    called_internally: bool = False
):
    """Closes the persistent Git Bash session and optionally notifies an admin."""
    global _persistent_git_bash_process, _persistent_git_bash_dlg, _persistent_git_bash_app
    logger.info("Attempting to close persistent Git Bash session...")
    
    proc = _persistent_git_bash_process
    session_active_at_call = False

    if proc and proc.pid is not None and proc.poll() is None:
        session_active_at_call = True
        logger.info(f"Active Git Bash process (PID: {proc.pid}) found. Closing.")
        if _persistent_git_bash_dlg and _persistent_git_bash_dlg.exists():
            try: _persistent_git_bash_dlg.close(); time.sleep(0.5)
            except Exception as e_close: logger.warning(f"Polite window close failed (PID {proc.pid}): {e_close}")
        
        if proc.poll() is None:
            logger.debug(f"Process (PID {proc.pid}) still active. Terminating.")
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: logger.warning(f"Process (PID {proc.pid}) timeout on terminate. Killing."); proc.kill(); proc.wait(timeout=2)
            except Exception as e_term: logger.error(f"Error terminating (PID {proc.pid}): {e_term}", exc_info=True)
        logger.info(f"Process (PID {proc.pid}) terminated with code: {proc.returncode}.")
    elif proc: logger.info(f"Git Bash process (PID: {proc.pid if proc.pid else 'N/A'}) already terminated.")
    else: logger.info("No Git Bash process to close.")

    _persistent_git_bash_process, _persistent_git_bash_dlg, _persistent_git_bash_app = None, None, None
    logger.info("Global Git Bash session variables reset.")

    if not called_internally and bot_token_for_notification and chat_id_for_notification:
        msg = "Persistent Git Bash session closed." if session_active_at_call else "No active Git Bash session found to close during cleanup."
        
        # Since the notification is primarily handled by the main bot's atexit registration,
        # we don't need to duplicate the direct requests call here.
        # If this module *were* to send its own independent notification, the try/except for requests would go here.
        # For now, we assume the caller (e.g., main bot's atexit) handles any necessary high-level notification.
        logger.debug(f"Session closure status for potential notification by caller: {msg}")
        # If you *did* want this module to send a notification independently sometimes,
        # you would re-add the requests call with its try/except here.
        # Example of re-adding if needed:
        # try:
        #     # Assuming CONFIG is somehow available or relevant params are passed:
        #     # from telegram_bot_controller import CONFIG # <-- This creates tight coupling, avoid if possible
        #     # api_base_url = str(CONFIG.telegram_bot.api_base_url) 
        #     # Alternatively, pass api_base_url as a parameter to this function if independent notification is desired
        #
        #     # Placeholder: If you had a self-contained way to get api_base_url
        #     # api_url_base_placeholder = "https://api.telegram.org/bot" 
        #     # url = f"{api_url_base_placeholder}{bot_token_for_notification}/sendMessage"
        #     # payload = {'chat_id': chat_id_for_notification, 'text': msg}
        #     # response = requests.post(url, data=payload, timeout=5)
        #     # if response.status_code == 200:
        #     #     logger.info(f"Independent shutdown notification (Git Bash session status) sent to Telegram chat ID {chat_id_for_notification}.")
        #     # else:
        #     #     logger.error(f"Failed to send independent shutdown notification. Status: {response.status_code}, Text: {response.text}")
        #     pass # Current decision: Let main bot handle notifications
        # except Exception as e_requests_internal:
        #     logger.error(f"Exception sending independent shutdown notification: {e_requests_internal}", exc_info=True)

    # No 'try' block here means no 'except' or 'finally' is needed for this specific part.