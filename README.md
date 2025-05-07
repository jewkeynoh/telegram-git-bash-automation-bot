# 🧠 Telegram Git Bash Automation Bot

A powerful Python-based Telegram bot that enables remote automation of Git Bash commands through a secure chat interface. Built for Windows environments, this bot leverages `python-telegram-bot` for API interaction and `pywinauto` for persistent GUI-based command execution in Git Bash.

---

## ✨ Features

- **🔐 Secure Access**: Restricts usage to authorized Telegram User IDs via `config.yaml`.
- **⚙️ Persistent Git Bash Session**: Maintains a live window, avoiding relaunch overhead.
- **📝 Custom Command Execution**:
  - `/exec <commands>`: Run semicolon-separated commands in the current working directory.
  - `/runcwd <PATH> :: <commands>`: Run commands in a one-time specified Windows directory.
- **📂 Directory Management**:
  - `/setdir <project_key>`: Set the active directory from predefined shortcuts.
  - `/currentdir`: Display the current working directory.
  - `/cleardir`: Reset to the default directory.
- **🚀 Predefined Git Tasks**:
  - `/git_checkout_staging`: Check out the staging branch and show status.
  - `/git_pull_staging`: Pull the latest changes from staging and show status.
- **⚙️ Configurable via YAML**: Paths, timeouts, and user access are driven by `config.yaml`.
- **🔒 Environment Variables**: Telegram token securely loaded from `.env`.
- **🧾 Logging**: Full activity logging to both console and file.
- **📣 Admin Alerts**: Sends lifecycle and critical errors to admin via Telegram.
- **🛑 Robust Error Handling**: Graceful error capture using global hooks and shutdown handlers.

---

## 📁 Project Structure

```
.
├── telegram_bot_controller.py       # Telegram command handling and main bot logic
├── automate_git_bash_actions.py    # Git Bash automation using pywinauto
├── config.yaml                     # Central configuration file
├── .env_example                    # Template for environment variables
├── requirements.txt                # Python dependencies
├── logs/
│   └── telegram_automation.log     # Bot log file
```

---

## ⚙️ Prerequisites

- **OS**: Windows
- **Python**: 3.8 or higher
- **Git for Windows**: Required (`git-bash.exe`)
- **Telegram Bot Token**: Get from [@BotFather](https://t.me/BotFather)
- **Telegram User ID**: Use [@userinfobot](https://t.me/userinfobot)

---

## 🚀 Setup & Installation

1. **Clone or Set Up the Project**:
    ```bash
    git clone <your-repository-url>
    cd <your-project-directory>
    ```

2. **Create and Activate a Virtual Environment**:
    ```bash
    python -m venv venv
    venv\Scripts\activate  # On Windows
    ```

3. **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

4. **Configure `.env`**:
    Create a `.env` file in the root directory:
    ```env
    TELEGRAM_BOT_TOKEN="YOUR_ACTUAL_TELEGRAM_BOT_TOKEN"
    ```

5. **Set Up `config.yaml`**:
    Customize the values:
    - `executable_path`: Full path to `git-bash.exe`
    - `default_target_directory_windows`
    - `authorized_user_ids`: Your Telegram User ID(s)
    - `project_paths`: Shortcuts for `/setdir`

---

## ▶️ Running the Bot

```bash
python telegram_bot_controller.py
```

If configured properly, you’ll receive a startup message in Telegram.

---

## 💬 Available Commands

> *(Refer to `help_command` in your codebase for updates.)*

### 🔧 General

- `/start` – Welcome message and keyboard
- `/help` – List of all commands

### 📂 Directory Commands

- `/setdir <key>` – Use a predefined project path
- `/currentdir` – Show current directory
- `/cleardir` – Reset to default

### 🖥️ Command Execution

- `/exec <cmd1>;...` – Execute in current directory
- `/runcwd <PATH> :: <cmd1>;...` – Execute in one-time path

### 🔄 Git Tasks

- `/git_checkout_staging`
- `/git_pull_staging`

### 📜 Logs

- `/get_logs [N]` – Last N lines of logs (default 20)

---

## 🐞 Troubleshooting

- **Git Bash Not Found**: Check `executable_path` in `config.yaml`.
- **Window Not Connecting**: Adjust `window_class_name` or delays in config.
- **Telegram Errors**:
  - Ensure you’ve started a chat with your bot
  - Escape special characters for MarkdownV2 if needed

---

## 📦 Contributing

Bug reports, feature requests, and pull requests are welcome!

---

## 📄 License

_This project is for personal use._  
(Or specify: “Licensed under MIT. See LICENSE.md.”)

---

## 🛑 Reminder

Be cautious with `/exec` and `/runcwd` commands — they run raw Bash input.

