# telegram_bot_controller.py
"""
Main controller for the Telegram Bot automating Git Bash actions.
Initializes the bot, loads configuration, sets up logging, defines command handlers,
and manages Git Bash command execution via a separate thread.
"""

import logging
import yaml
from pathlib import Path
import os
import sys
import threading
import time
import re
import asyncio
from dotenv import load_dotenv
import atexit
import traceback
import requests
import functools
from typing import List, Dict, Optional, Any, Callable

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode as TGParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, JobQueue
)
from pydantic import BaseModel, Field, FilePath, DirectoryPath, HttpUrl, conint, confloat, constr, field_validator

try:
    from automate_git_bash_actions import execute_git_bash_commands, close_persistent_git_bash_session
except ImportError:
    print("[CRITICAL ERROR] automate_git_bash_actions.py not found. This is a fatal error.")
    sys.exit(1)

class AppConfig(BaseModel):
    bot_name: str = Field(default="GitBashAutomatorBot")

class PywinautoInteractionConfig(BaseModel):
    """Configuration specific to Pywinauto interaction nuances."""
    window_launch_wait_seconds: confloat(ge=1) = Field(12.0)
    window_connect_timeout_seconds: conint(ge=5) = Field(30)
    window_element_ready_timeout_seconds: conint(ge=1) = Field(10)
    pywinauto_backend: constr(pattern=r"^(win32|uia)$") = Field("win32")
    window_class_name: str = Field("CASCADIA_HOSTING_WINDOW_CLASS")
    window_title_regex: str = Field(".*MINGW64.*")

class GitBashAutomationConfig(BaseModel):
    executable_path: FilePath
    default_target_directory_windows: Optional[DirectoryPath] = None
    command_pause_seconds: confloat(ge=0.1) = Field(3.0)
    pywinauto_settings: PywinautoInteractionConfig = Field(default_factory=PywinautoInteractionConfig)

class TelegramBotConfig(BaseModel):
    api_base_url: HttpUrl = Field(default="https://api.telegram.org/bot")
    authorized_user_ids: List[conint(gt=0)]
    max_message_length: conint(gt=100) = Field(4096)
    formatted_message_length_buffer: conint(ge=0) = Field(200)

    @field_validator('authorized_user_ids')
    @classmethod
    def check_authorized_ids_not_empty(cls, v):
        if not v:
            raise ValueError("authorized_user_ids list cannot be empty.")
        return v

class LoggingConfig(BaseModel):
    log_file: str = Field("logs/telegram_automation.log")
    log_level: constr(pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$") = Field("INFO")
    # log_max_bytes: Optional[conint(gt=0)] = None # For future implementation
    # log_backup_count: Optional[conint(gt=0)] = None # For future implementation

class MainConfig(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    git_bash_automation: GitBashAutomationConfig
    telegram_bot: TelegramBotConfig
    project_paths: Dict[str, DirectoryPath] = Field(default_factory=dict)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

CONFIG: MainConfig

def load_configuration(config_file_path: str = 'config.yaml') -> MainConfig:
    global CONFIG
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            raw_config_data = yaml.safe_load(f)
        if not raw_config_data:
            raise ValueError("Configuration file is empty.")
        CONFIG = MainConfig(**raw_config_data)
        return CONFIG
    except FileNotFoundError:
        sys.stderr.write(f"[CRITICAL ERROR] Configuration file not found: {config_file_path}\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"[CRITICAL ERROR] Failed to load or validate configuration: {e}\n")
        sys.exit(1)

CONFIG = load_configuration()

log_cfg = CONFIG.logging
log_file_path = Path(log_cfg.log_file)
log_file_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, log_cfg.log_level.upper(), logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s',
    handlers=[logging.FileHandler(log_file_path, encoding='utf-8'), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

dotenv_path = Path(__file__).resolve().parent / ".env"
if dotenv_path.is_file():
    logger.info(f"Loading .env file from: {dotenv_path}")
    load_dotenv(dotenv_path=dotenv_path)
else:
    logger.warning(f".env file not found at {dotenv_path}. Ensure TELEGRAM_BOT_TOKEN is set via other means if not using .env.")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    logger.critical("FATAL: TELEGRAM_BOT_TOKEN environment variable not found.")
    sys.exit(1)

BOT_NAME = CONFIG.app.bot_name
AUTHORIZED_USER_IDS = CONFIG.telegram_bot.authorized_user_ids
PROJECT_PATHS_CONFIG = CONFIG.project_paths
TELEGRAM_API_FULL_URL_BASE = f"{str(CONFIG.telegram_bot.api_base_url)}{TELEGRAM_BOT_TOKEN}"
SAFE_MSG_MAX_LEN = CONFIG.telegram_bot.max_message_length - CONFIG.telegram_bot.formatted_message_length_buffer

user_current_directories: Dict[int, str] = {}


def authorized_only(func: Callable) -> Callable:
    """Decorator to restrict command access to users listed in `AUTHORIZED_USER_IDS`."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user:
            logger.warning(f"Update without effective_user in @authorized_only command ({func.__name__}).")
            return

        user_id = update.effective_user.id
        if user_id in AUTHORIZED_USER_IDS:
            logger.debug(f"User {user_id} AUTHORIZED for {func.__name__}.")
            return await func(update, context, *args, **kwargs)
        else:
            logger.warning(f"UNAUTHORIZED access: UserID {user_id} ({update.effective_user.full_name or 'N/A'}) for {func.__name__}.")
            await reply_if_possible(update, context, "Sorry, you are not authorized for this action.")
            return
    return wrapper

def escape_markdown_v2(text: Any) -> str:
    """Escapes text for Telegram's MarkdownV2 parse mode."""
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def reply_if_possible(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs: Any):
    """Helper to send replies, preferring `reply_text` over `send_message`."""
    try:
        if update.effective_message and hasattr(update.effective_message, 'reply_text'):
            await update.effective_message.reply_text(text, **kwargs)
        elif update.effective_chat and context.bot:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, **kwargs)
        else:
            logger.warning(f"Cannot reply: Missing effective_message/chat or bot instance. Text: {text[:100]}")
    except Exception as e:
        chat_id_log = update.effective_chat.id if update.effective_chat else "N/A"
        logger.error(f"Failed to send reply to chat {chat_id_log}. Error: {e}", exc_info=True)

async def _async_send_telegram_feedback_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue task to send messages from background threads asynchronously."""
    job_data = context.job.data if context.job else {}
    chat_id, text_to_send, parse_mode = job_data.get("chat_id"), job_data.get("text"), job_data.get("parse_mode")
    user_id_log = job_data.get("user_id_for_log", "UnknownUser")

    if chat_id and text_to_send:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode=parse_mode)
            logger.info(f"JobQueue: Feedback sent to user {user_id_log} (ChatID: {chat_id}).")
        except Exception as e:
            logger.error(f"JobQueue: Failed to send message to chat {chat_id} (User: {user_id_log}). ParseMode: {parse_mode}. Error: {e}", exc_info=True)
            if parse_mode:
                fallback_text = f"Error sending formatted feedback. Original error (brief): {str(e)[:100]}"
                try:
                    await context.bot.send_message(chat_id=chat_id, text=fallback_text)
                except Exception as plain_e:
                    logger.error(f"JobQueue: Fallback send also failed for chat {chat_id}: {plain_e}", exc_info=True)
    else:
        logger.error(f"JobQueue: Missing chat_id or text for user {user_id_log}.")

def _run_automation_task_in_thread(
    target_function: Callable, git_bash_cfg: dict, commands_list: List[str],
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Worker for blocking Git Bash tasks. Formats result and schedules feedback via JobQueue."""
    user_id_log = update.effective_user.id if update.effective_user else "UnknownUser"
    chat_id = update.effective_chat.id if update.effective_chat else None
    thread_id = threading.get_ident()
    logger.info(f"Thread (ID:{thread_id}): Starting automation for user {user_id_log}. Cmds: {commands_list}. Dir: {git_bash_cfg.get('target_directory_windows', 'Default')}")

    try:
        result_dict = target_function(git_bash_cfg, commands_list)
        logger.debug(f"Thread (ID:{thread_id}): Result from target_function: {result_dict}")

        feedback_parts = ["✅ Automation Task COMPLETED\\." if result_dict.get("success") else "❌ Automation Task FAILED\\."]
        feedback_parts.append(f"Target Directory: `{escape_markdown_v2(str(git_bash_cfg.get('target_directory_windows', 'Default')))}`")
        feedback_parts.append(f"Summary: {escape_markdown_v2(result_dict.get('message', 'No summary.'))}")
        
        if result_dict.get("error_details"):
            feedback_parts.append(f"Error Info: `{escape_markdown_v2(str(result_dict.get('error_details')))}`")
    
        command_execution_outputs = result_dict.get("details") 
        if command_execution_outputs:
            feedback_parts.append("\n*Command Outputs:*")
            for output_block_str in command_execution_outputs:
                output_lines = str(output_block_str).split('\n', 1)
                output_header_unescaped = ""
                actual_cmd_output_raw = str(output_block_str).strip() 
                
                if len(output_lines) > 1 and "--- Output for:" in output_lines[0]:
                    output_header_unescaped = output_lines[0].strip()
                    actual_cmd_output_raw = output_lines[1].strip() 

                if output_header_unescaped:
                    feedback_parts.append(f"\n_{escape_markdown_v2(output_header_unescaped)}_") 

                if '```' in actual_cmd_output_raw:
                    safe_output_for_code_block = actual_cmd_output_raw.replace('```', '`` ` ``')
                else:
                    safe_output_for_code_block = actual_cmd_output_raw
                
                MAX_INDIVIDUAL_OUTPUT_LENGTH = 3500
                if len(safe_output_for_code_block) > MAX_INDIVIDUAL_OUTPUT_LENGTH:
                    display_output = safe_output_for_code_block[:MAX_INDIVIDUAL_OUTPUT_LENGTH]
                    last_newline_in_display = display_output.rfind('\n')
                    if last_newline_in_display > MAX_INDIVIDUAL_OUTPUT_LENGTH / 2:
                        display_output = display_output[:last_newline_in_display]
                    
                    feedback_parts.append(f"```\n{display_output}\n```")
                    feedback_parts.append(escape_markdown_v2("...(output for this command truncated)..."))
                else:
                    feedback_parts.append(f"```\n{safe_output_for_code_block}\n```")

        if result_dict.get("details"):
            feedback_parts.append("\nExecution Details:")
            feedback_parts.extend([f"  \\- `{escape_markdown_v2(str(d))}`" for d in result_dict.get("details", [])])

        full_feedback_text = "\n".join(feedback_parts)
        if len(full_feedback_text) > SAFE_MSG_MAX_LEN:
            trunc_msg = escape_markdown_v2("\n\n... (message truncated)")
            full_feedback_text = full_feedback_text[:SAFE_MSG_MAX_LEN - len(trunc_msg)] + trunc_msg
        
        logger.debug(f"Thread (ID:{thread_id}): Final feedback (len {len(full_feedback_text)}): >>>{full_feedback_text[:100]}...<<<")

        if chat_id and context.application and context.application.job_queue:
            job_data = {"chat_id": chat_id, "text": full_feedback_text, "parse_mode": TGParseMode.MARKDOWN_V2, "user_id_for_log": user_id_log}
            context.application.job_queue.run_once(_async_send_telegram_feedback_job, 0, data=job_data, name=f"feedback_{user_id_log}_{time.time()}")
            logger.info(f"Thread (ID:{thread_id}): Feedback job scheduled for user {user_id_log}.")
        else:
            logger.error(f"Thread (ID:{thread_id}): Cannot schedule feedback. Missing components (chat_id, app, job_queue). User: {user_id_log}.")

    except Exception as e:
        logger.error(f"Thread (ID:{thread_id}): Unhandled exception in _run_automation_task_in_thread for user {user_id_log}: {e}", exc_info=True)
        if chat_id and context.application and context.application.job_queue:
            err_text = f"🆘 Critical error in automation thread: {escape_markdown_v2(str(e)[:200])}"
            err_job_data = {"chat_id": chat_id, "text": err_text, "parse_mode": TGParseMode.MARKDOWN_V2, "user_id_for_log": user_id_log}
            context.application.job_queue.run_once(_async_send_telegram_feedback_job, 0, data=err_job_data, name=f"crit_err_{user_id_log}_{time.time()}")

@authorized_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start: Greets user and shows main keyboard."""
    user = update.effective_user
    logger.info(f"/start by user {user.id} ({user.full_name or 'N/A'}).")

    project_keys_list = list(PROJECT_PATHS_CONFIG.keys())
    keys = []

    row_size = 3
    for i in range(0, len(project_keys_list), row_size):
        keys.append([KeyboardButton(f"/setdir {k}") for k in project_keys_list[i:i + row_size]])
    
    keys.extend([
        [KeyboardButton("/currentdir"), KeyboardButton("/cleardir")],
        [KeyboardButton("/git_checkout_staging"), KeyboardButton("/git_pull_staging")],
        [KeyboardButton("/get_logs 20"), KeyboardButton("/reset_bash_session")],
        [KeyboardButton("/help")]
    ])

    reply_markup = ReplyKeyboardMarkup(keys, resize_keyboard=True, one_time_keyboard=False)

    line_with_periods_escaped = "Use `/setdir <project_key>`, then `/exec <commands>` or predefined tasks\\. See `/help`\\."
    
    start_text = (
        f"Hello {escape_markdown_v2(user.first_name)}\\! I'm {escape_markdown_v2(BOT_NAME)}\\.\n"
        f"{line_with_periods_escaped}" 
    )
    await reply_if_possible(update, context, start_text, reply_markup=reply_markup, parse_mode=TGParseMode.MARKDOWN_V2)

@authorized_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /help: Displays available commands and usage."""
    logger.info(f"/help by user {update.effective_user.id}.")

    prj_keys_list = list(PROJECT_PATHS_CONFIG.keys())
    if prj_keys_list:
        prj_keys = ", ".join(f"`{escape_markdown_v2(k)}`" for k in prj_keys_list)
    else:
        prj_keys = escape_markdown_v2("None configured.")

    help_text_lines = [
        "*🤖 Bot Help*",
        "\n*Directory Management:*",
        f"`/setdir <project_key>` \\- Set active dir\\. Keys: {prj_keys}\\. Ex: `/setdir estore`",
        "`/currentdir` \\- Show active dir\\.",
        "`/cleardir` \\- Clear active dir\\.",
        
        "\n*Custom Execution \\(CAUTION\\):*",
        "`/exec <cmd1>;<cmd2>...` \\- Run in active/default dir\\. Ex: `/exec pwd;ls \\-al`",
        "`/runcwd <PATH> :: <cmd1>;...` \\- Run in specified Windows dir\\. Ex: `/runcwd C:/Temp :: git status`",
        
        "\n*Predefined Git Tasks:*",
        "`/git_checkout_staging` \\- `git checkout staging` & `git status`\\.",
        "`/git_pull_staging` \\- `git pull origin staging` & `git status`\\.",
        
        "\n*Utilities:*",
        "`/get_logs [N]` \\- Show last N log lines \\(default 20, max 200\\)\\.",
        "`/start` \\- Welcome & keyboard\\.",
        "`/help` \\- This message\\."
    ]
    help_text = "\n".join(help_text_lines)
    
    try:
        await reply_if_possible(update, context, help_text, parse_mode=TGParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error sending MarkdownV2 help: {e}", exc_info=True)

        plain_help = help_text
        for char_to_remove in ['\\', '*', '`']:
            plain_help = plain_help.replace(char_to_remove, '')
        
        await reply_if_possible(update, context, f"Formatted help failed. Plain text version:\n\n{plain_help}\n\nError details (brief): {str(e)[:100]}")

@authorized_only
async def get_logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /get_logs: Fetches and displays last N log lines."""
    logger.info(f"/get_logs by user {update.effective_user.id}, args: {context.args}.")
    num_lines, max_lines = 20, 200
    if context.args:
        try:
            req_lines = int(context.args[0])
            if 0 < req_lines <= max_lines: num_lines = req_lines
            else: await reply_if_possible(update, context, f"Lines must be 1-{max_lines}. Defaulting to {num_lines}.")
        except (ValueError, IndexError): await reply_if_possible(update, context, f"Invalid lines. Defaulting to {num_lines}.")

    log_file = Path(CONFIG.logging.log_file)
    if not log_file.is_file():
        await reply_if_possible(update, context, f"Log file not found: {log_file}"); return
    
    try:
        with open(log_file, 'r', encoding='utf-8') as f: lines = f.readlines()
        last_n = lines[-num_lines:]
        if not last_n: await reply_if_possible(update, context, "Log file empty."); return
        
        log_out = "".join(last_n).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        header = f"Last {len(last_n)} log lines from `{escape_markdown_v2(log_file.name)}`:\n"
        full_msg = f"{header}<pre>{log_out}</pre>"

        if len(full_msg) > SAFE_MSG_MAX_LEN:
            await reply_if_possible(update, context, header + "Log output too long, sending chunks:", parse_mode=TGParseMode.MARKDOWN_V2)
            chunk = ""
            for line_esc in (l.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') for l in last_n):
                if len("<pre>" + chunk + line_esc + "</pre>") > SAFE_MSG_MAX_LEN:
                    if chunk: await reply_if_possible(update, context, f"<pre>{chunk}</pre>", parse_mode=TGParseMode.HTML); await asyncio.sleep(0.3)
                    chunk = line_esc
                else: chunk += line_esc
            if chunk: await reply_if_possible(update, context, f"<pre>{chunk}</pre>", parse_mode=TGParseMode.HTML)
        else: await reply_if_possible(update, context, full_msg, parse_mode=TGParseMode.HTML)
    except Exception as e:
        logger.error(f"Error reading/sending log file {log_file}: {e}", exc_info=True)
        await reply_if_possible(update, context, f"Error fetching logs: {escape_markdown_v2(str(e))}", parse_mode=TGParseMode.MARKDOWN_V2)

async def _initiate_bash_command_execution(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    commands: List[str], dir_override: Optional[str], cmd_name: str
):
    user_id = update.effective_user.id
    git_bash_cfg_dict = CONFIG.git_bash_automation.model_dump(exclude={'pywinauto_settings'}) 
    git_bash_cfg_dict.update(CONFIG.git_bash_automation.pywinauto_settings.model_dump())

    final_dir = dir_override or user_current_directories.get(user_id, str(CONFIG.git_bash_automation.default_target_directory_windows) if CONFIG.git_bash_automation.default_target_directory_windows else None)
    git_bash_cfg_dict['target_directory_windows'] = final_dir
    dir_src = f"specified by {cmd_name}" if dir_override else ("user's CWD" if user_id in user_current_directories else "default app CWD")

    logger.info(f"{cmd_name.upper()}: User {user_id}. Cmds: {commands}. TargetDir: {final_dir} (Source: {dir_src}).")
    
    escaped_cmd_name = escape_markdown_v2(cmd_name)
    escaped_commands_str = escape_markdown_v2(str(commands))
    escaped_final_dir_str = escape_markdown_v2(str(final_dir or 'Git Bash Default'))

    reply_md = (
        f"Received `{escaped_cmd_name}`\\.\n"
        f"Cmds: `{escaped_commands_str}`\n"
        f"Dir: `{escaped_final_dir_str}`\n"
        f"Executing\\.\\.\\."
    )
    await reply_if_possible(update, context, reply_md, parse_mode=TGParseMode.MARKDOWN_V2)
    
    threading.Thread(target=_run_automation_task_in_thread, args=(execute_git_bash_commands, git_bash_cfg_dict, commands, update, context), daemon=True).start()

@authorized_only
async def exec_custom_commands_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await reply_if_possible(update, context, "Usage: /exec <cmd1>;<cmd2>..."); return
    cmds = [cmd.strip() for cmd in " ".join(context.args).split(';') if cmd.strip()]
    if not cmds: await reply_if_possible(update, context, "No valid commands."); return
    await _initiate_bash_command_execution(update, context, cmds, None, "/exec")

@authorized_only
async def runcwd_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await reply_if_possible(update, context, "Usage: /runcwd <PATH> :: <cmds>"); return
    parts = " ".join(context.args).split("::", 1)
    if len(parts) != 2: await reply_if_possible(update, context, "Invalid format. Use <PATH> :: <cmds>"); return
    win_dir, cmd_str = parts[0].strip(), parts[1].strip()
    if not win_dir: await reply_if_possible(update, context, "Path cannot be empty."); return
    if not re.match(r"^[a-zA-Z]:[\\/]", win_dir):
         await reply_if_possible(update, context, f"Path `{escape_markdown_v2(win_dir)}` doesn't look like a valid Windows absolute path.", parse_mode=TGParseMode.MARKDOWN_V2); return
    cmds = [cmd.strip() for cmd in cmd_str.split(';') if cmd.strip()]
    if not cmds: await reply_if_possible(update, context, "No valid commands after '::'."); return
    await _initiate_bash_command_execution(update, context, cmds, win_dir, "/runcwd")

@authorized_only
async def set_directory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_current_directories
    user_id = update.effective_user.id

    prj_keys_list = list(PROJECT_PATHS_CONFIG.keys())
    if prj_keys_list:
        keys_disp = ", ".join(f"`{escape_markdown_v2(k)}`" for k in prj_keys_list)
    else:
        keys_disp = escape_markdown_v2("None configured.") 
    
    if not context.args or len(context.args) != 1:
        reply_text = (
            f"Usage: `/setdir <project_key>`\n"
            f"Sets your active directory for commands like `/exec` and predefined Git tasks\\.\n"
            f"Available project keys: {keys_disp}"
        )
        await reply_if_possible(update, context, reply_text, parse_mode=TGParseMode.MARKDOWN_V2)
        return

    key_arg = context.args[0].lower()
    if key_arg in PROJECT_PATHS_CONFIG:
        path_obj = PROJECT_PATHS_CONFIG[key_arg]
        path_str = str(path_obj)

        user_current_directories[user_id] = path_str
        logger.info(f"User CWD SET: User {user_id} to '{key_arg}': {path_str}. All: {user_current_directories}")

        message_text = f"✅ Dir set to *{escape_markdown_v2(key_arg)}* \\(\\`{escape_markdown_v2(path_str)}\\`\\)"
        
        await reply_if_possible(
            update, context, 
            message_text,
            parse_mode=TGParseMode.MARKDOWN_V2
        )
    else:
        logger.warning(f"User {user_id} invalid project key: {key_arg}")
        message_text = f"❌ Key '`{escape_markdown_v2(key_arg)}`' not found\\. Keys: {keys_disp}"
        
        await reply_if_possible(
            update, context, 
            message_text,
            parse_mode=TGParseMode.MARKDOWN_V2
        )

@authorized_only
async def show_current_directory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    curr_path = user_current_directories.get(user_id)
    if curr_path:
        key = next((k for k, p in PROJECT_PATHS_CONFIG.items() if str(p) == curr_path), "Custom Path")
        await reply_if_possible(update, context, f"Current dir: *{escape_markdown_v2(key)}* (`{escape_markdown_v2(curr_path)}`)", parse_mode=TGParseMode.MARKDOWN_V2)
    else:
        def_dir = str(CONFIG.git_bash_automation.default_target_directory_windows) if CONFIG.git_bash_automation.default_target_directory_windows else "Git Bash Default"
        await reply_if_possible(update, context, f"No specific dir set. Using default: `{escape_markdown_v2(def_dir)}`", parse_mode=TGParseMode.MARKDOWN_V2)

@authorized_only
async def clear_directory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_current_directories
    user_id = update.effective_user.id
    if user_id in user_current_directories:
        path = user_current_directories.pop(user_id)
        logger.info(f"User CWD CLEARED: User {user_id} (was {path}). All: {user_current_directories}")
        def_dir = str(CONFIG.git_bash_automation.default_target_directory_windows) if CONFIG.git_bash_automation.default_target_directory_windows else "Git Bash Default"
        await reply_if_possible(update, context, f"✅ CWD (`{escape_markdown_v2(path)}`) cleared. Using default: `{escape_markdown_v2(def_dir)}`.", parse_mode=TGParseMode.MARKDOWN_V2)
    else: await reply_if_possible(update, context, "No specific CWD to clear.", parse_mode=TGParseMode.MARKDOWN_V2)


@authorized_only
async def reset_bash_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Closes the current persistent Git Bash session and allows it to be relaunched
    on the next command that requires it.
    """
    user = update.effective_user
    logger.info(f"/reset_bash_session command initiated by user {user.id} ({user.full_name or 'N/A'})")

    initial_message = "Attempting to reset the Git Bash session\\.\\.\\. Please wait\\.\\"
    await reply_if_possible(update, context, initial_message, parse_mode=TGParseMode.MARKDOWN_V2)

    try:
        def _do_close_session():
            logger.info("Thread: Calling close_persistent_git_bash_session for reset.")
            close_persistent_git_bash_session(called_internally=True)
            logger.info("Thread: close_persistent_git_bash_session completed for reset.")

            if update.effective_chat and context.application and context.application.job_queue:
                reset_done_msg_literal = "Git Bash session has been signaled to close. A new session will be started on the next relevant command."
                
                job_data = {
                    "chat_id": update.effective_chat.id,
                    "text": escape_markdown_v2(reset_done_msg_literal),
                    "parse_mode": TGParseMode.MARKDOWN_V2,
                    "user_id_for_log": user.id
                }
                context.application.job_queue.run_once(_async_send_telegram_feedback_job, 0, data=job_data, name=f"reset_confirm_{user.id}")
        
        thread = threading.Thread(target=_do_close_session, daemon=True)
        thread.start()
        
    except Exception as e:
        logger.error(f"Error during /reset_bash_session: {e}", exc_info=True)
        error_message_literal = f"An error occurred while trying to reset the session: {str(e)}"
        await reply_if_possible(update, context, escape_markdown_v2(error_message_literal), parse_mode=TGParseMode.MARKDOWN_V2)


@authorized_only
async def exec_here_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /exechere: Executes commands in the current Git Bash CWD without an initial CD."""
    user = update.effective_user
    logger.info(f"/exechere command invoked by user {user.id} with args: {context.args}")

    if not context.args:
        await reply_if_possible(update, context, "Usage: `/exechere <command1>;<command2>;...`\nExecutes in the *current* Git Bash working directory.", parse_mode=TGParseMode.MARKDOWN_V2)
        return

    full_command_string = " ".join(context.args)
    commands_to_execute = [cmd.strip() for cmd in full_command_string.split(';') if cmd.strip()]

    if not commands_to_execute:
        await reply_if_possible(update, context, "No valid commands provided for `/exechere`.", parse_mode=TGParseMode.MARKDOWN_V2)
        return

    git_bash_cfg_base = CONFIG.git_bash_automation.model_dump(exclude={'pywinauto_settings'}) 
    git_bash_cfg_for_thread = git_bash_cfg_base.copy()
    git_bash_cfg_for_thread.update(CONFIG.git_bash_automation.pywinauto_settings.model_dump())
    
    git_bash_cfg_for_thread['target_directory_windows'] = None

    logger.info(f"EXECHERE_HANDLER: User {user.id}. Cmds: {commands_to_execute}. Effective target_dir will be current Git Bash CWD.")
    
    reply_md = (
        f"Received `/exechere`\\.\n"
        f"Cmds: `{escape_markdown_v2(str(commands_to_execute))}`\n"
        f"Executing in current Git Bash working directory\\.\\.\\."
    )
    await reply_if_possible(update, context, reply_md, parse_mode=TGParseMode.MARKDOWN_V2)
    
    threading.Thread(
        target=_run_automation_task_in_thread, 
        args=(execute_git_bash_commands, git_bash_cfg_for_thread, commands_to_execute, update, context), 
        daemon=True
    ).start()


async def run_predefined_git_task(update: Update, context: ContextTypes.DEFAULT_TYPE, cmds: List[str], task_name: str):
    await _initiate_bash_command_execution(update, context, cmds, None, f"/{task_name.lower().replace(' ', '_')}")

@authorized_only
async def git_checkout_staging_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_predefined_git_task(update, context, ["git checkout staging", "git status"], "Checkout Staging")
@authorized_only
async def git_pull_staging_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_predefined_git_task(update, context, ["git pull origin staging", "git status"], "Pull Staging")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for python-telegram-bot."""
    logger.error(f"Exception during update handling (Update: {update}):", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        err_msg = f"🆘 Unexpected error: {type(context.error).__name__}. Admins notified."
        await reply_if_possible(update, context, escape_markdown_v2(err_msg), parse_mode=TGParseMode.MARKDOWN_V2)
    elif context.error and AUTHORIZED_USER_IDS:
        admin_err_msg = f"🆘 Bot Alert: Internal error (not user chat related).\nError: {type(context.error).__name__} - {str(context.error)[:200]}"
        _send_sync_telegram_message(admin_err_msg, AUTHORIZED_USER_IDS[0])

async def log_unmatched_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text and update.effective_user:
        msg_type = "COMMAND" if update.message.text.startswith('/') else "TEXT"
        logger.debug(f"Unmatched {msg_type}: '{update.message.text}' from User {update.effective_user.id}.")

def _send_sync_telegram_message(text: str, chat_id_override: Optional[int] = None):
    """Synchronously sends a Telegram message. For critical notifications (shutdown/crash)."""
    target_chat_id = chat_id_override or (AUTHORIZED_USER_IDS[0] if AUTHORIZED_USER_IDS else None)
    if not target_chat_id:
        logger.error(f"SYNC_MSG: No chat_id to send to. Message: {text[:50]}...")
        return
    
    url = f"{TELEGRAM_API_FULL_URL_BASE}/sendMessage"
    payload = {'chat_id': target_chat_id, 'text': text}
    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"SYNC_MSG: Sent to {target_chat_id}: '{text[:50]}...'")
        else:
            logger.error(f"SYNC_MSG: Failed to send to {target_chat_id}. Status: {response.status_code}, Text: {response.text}")
    except Exception as e:
        logger.error(f"SYNC_MSG: Exception sending to {target_chat_id}: {e}", exc_info=True)

def global_exception_handler(exc_type, exc_value, exc_tb):
    """Catches unhandled exceptions system-wide via `sys.excepthook`."""
    err_details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.critical(f"UNHANDLED SYS.EXCEPTHOOK EXCEPTION:\n{err_details}")
    crash_msg = f"🆘 CRITICAL SYS ERROR: Bot '{BOT_NAME}' crashed!\nType: {exc_type.__name__}\nError: {str(exc_value)[:200]}"
    _send_sync_telegram_message(crash_msg)

def main_bot_application():
    logger.info(f"--- Initializing Bot: {BOT_NAME} ---")
    logger.info(f"Bot Name: {BOT_NAME}, Authorized IDs: {AUTHORIZED_USER_IDS}")
    logger.info(f"Default Git Dir: {CONFIG.git_bash_automation.default_target_directory_windows or 'Session Default'}")
    logger.info(f"Project Paths: {list(PROJECT_PATHS_CONFIG.keys())}")

    sys.excepthook = global_exception_handler
    
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).job_queue(JobQueue()).build()
    
    atexit.register(close_persistent_git_bash_session, TELEGRAM_BOT_TOKEN, AUTHORIZED_USER_IDS[0] if AUTHORIZED_USER_IDS else None)
    atexit.register(_send_sync_telegram_message, f"🤖 Bot '{BOT_NAME}' shutting down (atexit).")

    handlers = [
        CommandHandler('start', start_command), 
        CommandHandler('help', help_command),
        CommandHandler('exec', exec_custom_commands_handler), 
        CommandHandler('get_logs', get_logs_command),
        CommandHandler('runcwd', runcwd_command_handler), 
        CommandHandler('setdir', set_directory_command),
        CommandHandler('currentdir', show_current_directory_command), 
        CommandHandler('cleardir', clear_directory_command),
        CommandHandler('git_checkout_staging', git_checkout_staging_command),
        CommandHandler('git_pull_staging', git_pull_staging_command),
        CommandHandler('reset_bash_session', reset_bash_session_command),
        CommandHandler('exechere', exec_here_command_handler),
    ]
    application.add_handlers(handlers)

    unmatched_handler = MessageHandler(filters.COMMAND | (filters.TEXT & ~filters.COMMAND), log_unmatched_messages)
    application.add_handler(unmatched_handler, group=1)

    application.add_error_handler(error_handler)

    logger.info("Telegram bot starting polling...")
    _send_sync_telegram_message(f"🚀 Bot '{BOT_NAME}' starting up!")

    try:
        application.run_polling()
    except KeyboardInterrupt:
        logger.info(f"Bot '{BOT_NAME}' received Ctrl+C. Shutting down...")
    except Exception as e:
        logger.critical(f"Bot polling loop CRASHED: {e}", exc_info=True)
        _send_sync_telegram_message(f"🆘 CRITICAL POLLING FAILURE: Bot '{BOT_NAME}' crashed! Error: {type(e).__name__} - {str(e)[:100]}")
    finally:
        logger.info("Bot polling loop ended.")

if __name__ == '__main__':
    main_bot_application()
    logger.info(f"--- {BOT_NAME} Application Exited ---")