import json
import secrets
import logging
import datetime
import hmac
import hashlib
import requests
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
import jwt
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from sqlalchemy import select, func, and_
from sqlalchemy.orm import selectinload

import datetime
import jwt
import requests
from bot.config import config
from bot.db import get_db_session, AsyncSessionLocal
from bot.models import User, Task, TaskAssignment, TaskModule, AssessmentScore, SessionUsage
from bot.utils import rate_limit, robust_handler
from bot.shared_utils import get_user_by_telegram_id, generate_webapp_url

# Import additional UX features
from bot.features import *

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Helper Functions ---

def validate_telegram_data(init_data: str, bot_token: str) -> bool:
    """
    Validates the data received from the Telegram Web App.
    """
    try:
        parsed_data = dict(item.split("=") for item in init_data.split("&"))
        hash_value = parsed_data.pop("hash")
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed_data.items())
        )
        secret_key = hmac.new(
            key=b"WebAppData", msg=bot_token.encode(), digestmod=hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            key=secret_key, msg=data_check_string.encode(), digestmod=hashlib.sha256
        ).hexdigest()
        return calculated_hash == hash_value
    except Exception:
        return False



# --- Handlers ---

@robust_handler
@rate_limit
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message and role selection if not registered."""
    telegram_user = update.effective_user
    
    # Use robust session generator
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        
        if user:
            # User exists, show main menu with a switch role hint
            await update.message.reply_text(
                f"Welcome back {telegram_user.first_name}! You are currently logged in as { 'Teacher' if user.is_teacher else 'Student' }.\n" \
                "Use /switchrole to change this role at any time."
            )
            await show_main_menu(update, context, user)
        else:
            # New user, ask for role
            keyboard = [
                [
                    InlineKeyboardButton("👨‍🏫 Teacher", callback_data="role_teacher"),
                    InlineKeyboardButton("👨‍🎓 Student", callback_data="role_student"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"Welcome {telegram_user.first_name}! Please select your role to continue:",
                reply_markup=reply_markup,
            )

@robust_handler
@rate_limit
async def role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles role selection."""
    query = update.callback_query
    await query.answer()
    
    # Validate callback data
    data_parts = query.data.split("_")
    if len(data_parts) < 2 or data_parts[1] not in ["teacher", "student"]:
         await query.edit_message_text("Invalid role selection.")
         return

    role = data_parts[1]
    telegram_user = query.from_user
    
    async for session in get_db_session():
        user = await create_or_update_user(session, telegram_user, role)

        # Provide extra clarity on logout and role switching
        msg = f"Role set to: {role.capitalize()}!"
        msg += "\n\n" if role else ""
        msg += "You can use /switchrole any time to change your role."
        await query.edit_message_text(msg)
        await show_main_menu(update, context, user)


async def how_to_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles howto role callback selection."""
    query = update.callback_query
    await query.answer()

    content_map = {
        'howto_teacher': (
            '👨‍🏫 *Teacher How-To Guide*\n\n'
            '1️⃣ Open App and log in via Telegram.\n'
            '2️⃣ On first login, select *Teacher* role.\n'
            '3️⃣ In web app, go to *Tasks* to create modules and assign to students.\n'
            '4️⃣ Use *Analytics* to review class/individual progress.\n'
            '5️⃣ If you terminate the session, use /switchrole in bot to re-select role.\n\n'
            '💡 Tip: Each student completion refunds one credit to your balance.'
        ),
        'howto_student': (
            '👨‍🎓 *Student How-To Guide*\n\n'
            '1️⃣ Open App and log in via Telegram.\n'
            '2️⃣ On first login, select *Student* role.\n'
            '3️⃣ Join teacher with invite token from teacher settings.\n'
            '4️⃣ In web app, go to *Tasks* and do assigned work.\n'
            '5️⃣ Complete task to get AI feedback and preserve progress.\n\n'
            '💡 Tip: Tap *Practice* for speech/writing workouts, and *Analytics* to track your score growth.'
        )
    }

    text = content_map.get(query.data, '❌ Unknown selection. Please use /switchrole or /start to try again.')

    # Add navigation buttons
    keyboard = []
    if query.data == 'howto_teacher':
        keyboard = [[InlineKeyboardButton("👨‍🎓 Student Guide →", callback_data="howto_student")]]
    elif query.data == 'howto_student':
        keyboard = [[InlineKeyboardButton("← 👨‍🏫 Teacher Guide", callback_data="howto_teacher")]]

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User):
    """Displays the main menu based on user role."""

    # Web App Button
    web_app = WebAppInfo(url=generate_webapp_url(user))

    keyboard = [
        [KeyboardButton("📱 Open App", web_app=web_app)],
        [KeyboardButton("📊 Progress"), KeyboardButton("📝 Tasks")],
        [KeyboardButton("❓ How To"), KeyboardButton("💳 Buy Credits / Contact Admin")]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    msg = "Welcome back! Use the buttons below to navigate."

    # Add inline quick access buttons
    quick_access_keyboard = []
    if user.is_teacher:
        quick_access_keyboard = [
            [InlineKeyboardButton("📊 Quick Class Stats", callback_data="quick_class_stats")],
            [InlineKeyboardButton("⚙️ Account Settings", callback_data="quick_settings")]
        ]
    else:
        quick_access_keyboard = [
            [InlineKeyboardButton("📈 My Progress", callback_data="quick_student_progress")],
            [InlineKeyboardButton("⚙️ Account Settings", callback_data="quick_settings")]
        ]

    inline_reply_markup = InlineKeyboardMarkup(quick_access_keyboard) if quick_access_keyboard else None

    if update.message:
        if inline_reply_markup:
            await update.message.reply_text(msg, reply_markup=inline_reply_markup)
        else:
            await update.message.reply_text(msg, reply_markup=reply_markup)
    elif update.callback_query:
        # If coming from callback, we need to send a new message
        if inline_reply_markup:
            await context.bot.send_message(chat_id=user.telegram_id, text=msg, reply_markup=inline_reply_markup)
        else:
            await context.bot.send_message(chat_id=user.telegram_id, text=msg, reply_markup=reply_markup)

@robust_handler
@rate_limit
async def switch_role_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows existing users to choose teacher/student again."""
    telegram_user = update.effective_user

    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await update.message.reply_text("Please /start first to register.")
            return

        current_role = "Teacher" if user.is_teacher else "Student"
        current_role_emoji = "👨‍🏫" if user.is_teacher else "👨‍🎓"

        keyboard = [
            [
                InlineKeyboardButton("👨‍🏫 Switch to Teacher", callback_data="confirm_switch_teacher"),
                InlineKeyboardButton("👨‍🎓 Switch to Student", callback_data="confirm_switch_student"),
            ],
            [
                InlineKeyboardButton(f"{current_role_emoji} Current: {current_role}", callback_data="current_role_info"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"🔄 **Role Switching**\n\n"
            f"You are currently a **{current_role}**.\n"
            f"Choose your new role below:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

@robust_handler
@rate_limit
async def progress_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Progress button."""
    telegram_user = update.effective_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await update.message.reply_text("Please /start first.")
            return

        if user.is_teacher:
            # Teacher: List students
            result = await session.execute(
                select(User).where(User.assigned_teacher_id == user.id)
            )
            students = result.scalars().all()
            
            keyboard = []
            for student in students:
                keyboard.append([InlineKeyboardButton(student.username, callback_data=f"prog_stu_{student.id}")])
            
            keyboard.append([InlineKeyboardButton("📊 Overall Class Progress", callback_data="prog_class_overall")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Select a student to view progress:", reply_markup=reply_markup)
            
        else:
            # Student: Show comprehensive real progress
            total_tasks_result = await session.execute(
                select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id == user.id)
            )
            total_tasks = total_tasks_result.scalar() or 0
            
            completed_tasks_result = await session.execute(
                select(func.count(TaskAssignment.id)).where(
                    and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == True)
                )
            )
            completed_tasks = completed_tasks_result.scalar() or 0
            
            percentage = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
            
            # Calculate average score from completed assignments
            avg_score_res = await session.execute(
                select(func.avg(AssessmentScore.overall_score))
                .join(SessionUsage, AssessmentScore.session_usage_id == SessionUsage.id)
                .where(SessionUsage.user_id == user.id)
            )
            avg_score = avg_score_res.scalar() or 0.0
            
            # Count practice sessions
            practice_sessions_res = await session.execute(
                select(func.count(SessionUsage.id)).where(SessionUsage.user_id == user.id)
            )
            practice_sessions = practice_sessions_res.scalar() or 0
            
            # Calculate total practice time (assuming 15 minutes per session)
            total_minutes = practice_sessions * 15
            hours = total_minutes // 60
            minutes = total_minutes % 60
            
            # Next pending module/task
            next_task_result = await session.execute(
                select(Task).join(TaskAssignment).where(
                    and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == False)
                ).order_by(Task.due_date.asc()).limit(1)
            )
            next_task = next_task_result.scalars().first()
            
            msg = f"📊 **Your Real Progress**\n\n"
            msg += f"📈 *Completion Rate:* {percentage:.1f}%\n"
            msg += f"✅ *Modules Completed:* {completed_tasks}/{total_tasks}\n"
            msg += f"⭐ *Average Score:* {avg_score:.1f}/9.0\n"
            msg += f"🎯 *Practice Sessions:* {practice_sessions}\n"
            msg += f"⏱️ *Total Practice Time:* {hours}h {minutes}m\n\n"
            
            if next_task:
                msg += f"📌 *Next Task:* {next_task.title}\n"
                msg += f"📅 *Due Date:* {next_task.due_date.strftime('%Y-%m-%d')}"
            else:
                msg += "🎉 *All tasks completed!*"
                
            await update.message.reply_markdown(msg)

@robust_handler
@rate_limit
async def student_progress_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher views specific student progress."""
    query = update.callback_query
    await query.answer()
    
    try:
        student_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid student selection.")
        return
    
    async for session in get_db_session():
        student = await session.get(User, student_id)
        if not student:
            await query.edit_message_text("Student not found.")
            return
            
        # Calc stats
        total_tasks_res = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id == student.id))
        total_tasks = total_tasks_res.scalar() or 0
        
        completed_tasks_res = await session.execute(select(func.count(TaskAssignment.id)).where(and_(TaskAssignment.student_id == student.id, TaskAssignment.completed == True)))
        completed_tasks = completed_tasks_res.scalar() or 0
        
        percentage = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
        
        # Average score
        avg_score_res = await session.execute(
            select(func.avg(AssessmentScore.overall_score))
            .join(SessionUsage, AssessmentScore.session_usage_id == SessionUsage.id)
            .where(SessionUsage.user_id == student.id)
        )
        avg_score = avg_score_res.scalar() or 0.0
        
        msg = f"👤 **Student: {student.username}**\n\n"
        msg += f"📊 Completion Rate: {percentage:.1f}%\n"
        msg += f"⭐ Average Score: {avg_score:.1f}/9.0\n"
        msg += f"📅 Last Active: {student.last_login.strftime('%Y-%m-%d %H:%M') if student.last_login else 'Never'}\n"
        msg += f"📝 Total Tasks: {total_tasks}"

        # Add drill-down action buttons
        keyboard = [
            [InlineKeyboardButton("📋 View Student Tasks", callback_data=f"student_tasks_{student_id}")],
            [InlineKeyboardButton("📈 View Student Analytics", callback_data=f"student_analytics_{student_id}")],
            [InlineKeyboardButton("⚖️ Compare with Class", callback_data=f"student_compare_{student_id}")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="progress_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def generate_token_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher command: /generate_token - Creates a one-time invite token for students."""
    telegram_user = update.effective_user

    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await update.message.reply_text("Please /start first to register.")
            return

        if not user.is_teacher:
            await update.message.reply_text("❌ This command is only for teachers.")
            return

        # Show token generation options
        keyboard = [
            [
                InlineKeyboardButton("👥 Small Class (10 users, 7 days)", callback_data="token_gen_small"),
                InlineKeyboardButton("📊 Medium Class (30 users, 30 days)", callback_data="token_gen_medium"),
            ],
            [
                InlineKeyboardButton("🏢 Large Group (100 users, 90 days)", callback_data="token_gen_large"),
                InlineKeyboardButton("⚙️ Custom", callback_data="token_gen_custom"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🔐 **Generate Student Invite Token**\n\n"
            "Select token configuration:\n"
            "• Small: 10 max students, expires in 7 days\n"
            "• Medium: 30 max students, expires in 30 days\n"
            "• Large: 100 max students, expires in 90 days\n"
            "• Custom: Configure your own limits",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

@robust_handler
@rate_limit
async def token_gen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle token generation with preset or custom options."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    option = query.data.split("_")[2]  # small, medium, large, or custom

    # Define presets
    presets = {
        "small": {"max_uses": 10, "expires_in_days": 7},
        "medium": {"max_uses": 30, "expires_in_days": 30},
        "large": {"max_uses": 100, "expires_in_days": 90},
    }

    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await query.edit_message_text("❌ User not found. Please /start first.")
            return

        if option == "custom":
            # Start conversation for custom values
            context.user_data['generating_custom_token'] = True
            await query.edit_message_text(
                "Enter custom token settings in format: `max_users days`\n"
                "Example: `25 14` (25 students, expires in 14 days)\n\n"
                "Reply with your settings:"
            )
            return

        # Use preset
        if option not in presets:
            await query.edit_message_text("❌ Invalid option.")
            return

        config_data = presets[option]
        await _call_backend_token_generation(query, user, config_data)

async def _call_backend_token_generation(query, user, config_data: dict):
    """Call backend API to generate the token."""
    try:
        import requests
        import datetime
        import jwt
        from bot.config import config

        # Get JWT token for backend authentication
        payload = {
            'user_id': user.id,
            'is_teacher': True,
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        }
        jwt_token = jwt.encode(payload, config.SECRET_KEY, algorithm='HS256')

        response = requests.post(
            f"{config.BACKEND_API_URL}/teacher/generate_token",
            json={
                "max_uses": config_data["max_uses"],
                "expires_in_days": config_data["expires_in_days"]
            },
            headers={"Authorization": f"Bearer {jwt_token}"},
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            token = data.get("token")

            msg = f"✅ **Token Generated Successfully!**\n\n"
            msg += f"🔑 Token: `{token}`\n\n"
            msg += f"📊 Settings:\n"
            msg += f"• Max Students: {config_data['max_uses']}\n"
            msg += f"• Expires: {config_data['expires_in_days']} days\n\n"
            msg += f"📋 Share this token with your students.\n"
            msg += f"They can use the `/join_teacher` command to join."

            # Add action buttons
            keyboard = [
                [InlineKeyboardButton("📋 Copy Token", callback_data=f"token_copy_{token}")],
                [InlineKeyboardButton("📤 Share Instructions", callback_data="token_share_help")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)
        else:
            error_msg = response.json().get("error", "Unknown error")
            await query.edit_message_text(f"❌ Failed to generate token: {error_msg}")

    except requests.exceptions.Timeout:
        await query.edit_message_text("⏱️ Request timeout. Please try again.")
    except Exception as e:
        logger.error(f"Token generation error: {e}")
        await query.edit_message_text(f"❌ Error: {str(e)}")

@robust_handler
@rate_limit
async def join_teacher_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Student command: /join_teacher - Join a teacher using a token."""
    telegram_user = update.effective_user

    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await update.message.reply_text("Please /start first to register.")
            return

        if user.is_teacher:
            await update.message.reply_text("❌ Teachers cannot join other teachers.")
            return

        # Prompt for token
        context.user_data['joining_teacher'] = True
        await update.message.reply_text(
            "👨‍🏫 **Join a Teacher's Group**\n\n"
            "Please send the invitation token from your teacher:\n\n"
            "_Send the token code now:_",
            parse_mode='Markdown'
        )

@robust_handler
@rate_limit
async def tasks_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the 'Go Back' button from task analytics."""
    query = update.callback_query
    await query.answer()

    # Call the tasks handler again to show the tasks list
    # We need to pass the user from the query
    telegram_user = query.from_user

    # Simulate calling tasks_handler with the same parameters
    # We'll create a mock update object with the callback query
    mock_update = type('MockUpdate', (), {})()
    mock_update.effective_user = telegram_user
    mock_update.message = type('MockMessage', (), {})()
    mock_update.message.reply_text = lambda text, reply_markup=None: query.edit_message_text(text=text, reply_markup=reply_markup)
    mock_update.callback_query = query

    # Call tasks handler
    await tasks_handler(mock_update, context)

@robust_handler
@rate_limit
async def progress_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the 'Go Back' button from student progress detail."""
    query = update.callback_query
    await query.answer()

    # Call the progress handler again to show the progress menu
    # We need to pass the user from the query
    telegram_user = query.from_user

    # Simulate calling progress_handler with the same parameters
    # We'll create a mock update object with the callback query
    mock_update = type('MockUpdate', (), {})()
    mock_update.effective_user = telegram_user
    mock_update.message = type('MockMessage', (), {})()
    mock_update.message.reply_text = lambda text, reply_markup=None: query.edit_message_text(text=text, reply_markup=reply_markup)
    mock_update.callback_query = query

    # Call progress handler
    await progress_handler(mock_update, context)

@robust_handler
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (custom token settings, join token)."""
    telegram_user = update.effective_user
    message_text = update.message.text

    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)

        # Handle custom token generation
        if context.user_data.get('generating_custom_token'):
            context.user_data['generating_custom_token'] = False

            try:
                parts = message_text.split()
                if len(parts) != 2:
                    await update.message.reply_text("❌ Invalid format. Use: `max_users days`")
                    return

                max_users = int(parts[0])
                expires_days = int(parts[1])

                if max_users < 1 or max_users > 1000:
                    await update.message.reply_text("❌ Max users must be between 1 and 1000.")
                    return

                if expires_days < 1 or expires_days > 365:
                    await update.message.reply_text("❌ Expiration days must be between 1 and 365.")
                    return

                config_data = {"max_uses": max_users, "expires_in_days": expires_days}

                # Call backend
                payload = {
                    'user_id': user.id,
                    'is_teacher': True,
                    'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)
                }
                jwt_token = jwt.encode(payload, config.SECRET_KEY, algorithm='HS256')

                response = requests.post(
                    f"{config.BACKEND_API_URL}/teacher/generate_token",
                    json=config_data,
                    headers={"Authorization": f"Bearer {jwt_token}"},
                    timeout=10
                )

                if response.status_code == 200:
                    data = response.json()
                    token = data.get("token")
                    msg = f"✅ **Token Generated!**\n\n"
                    msg += f"🔑 Token: `{token}`\n"
                    msg += f"👥 Max Students: {max_users}\n"
                    msg += f"📅 Expires: {expires_days} days"
                    await update.message.reply_text(msg, parse_mode='Markdown')
                else:
                    await update.message.reply_text("❌ Failed to generate token.")

            except ValueError:
                await update.message.reply_text("❌ Invalid input. Use: `max_users days` (numbers only)")
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {str(e)}")

        # Handle join teacher token
        elif context.user_data.get('joining_teacher'):
            context.user_data['joining_teacher'] = False
            token = message_text.strip()

            try:
                payload = {
                    'user_id': user.id,
                    'is_teacher': False,
                    'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)
                }
                jwt_token = jwt.encode(payload, config.SECRET_KEY, algorithm='HS256')

                response = requests.post(
                    f"{config.BACKEND_API_URL}/teacher/join",
                    json={"teacher_token": token},
                    headers={"Authorization": f"Bearer {jwt_token}"},
                    timeout=10
                )

                if response.status_code == 200:
                    data = response.json()
                    teacher = data.get("teacher", {})
                    msg = f"✅ **Successfully Joined!**\n\n"
                    msg += f"👨‍🏫 Teacher: {teacher.get('username', 'Unknown')}\n"
                    msg += f"📧 Email: {teacher.get('email', 'N/A')}\n\n"
                    msg += "You can now receive assignments and feedback from your teacher!"
                    await update.message.reply_text(msg, parse_mode='Markdown')
                else:
                    error_msg = response.json().get("error", "Invalid or expired token")
                    await update.message.reply_text(f"❌ {error_msg}")

            except requests.exceptions.Timeout:
                await update.message.reply_text("⏱️ Request timeout. Please try again.")
            except Exception as e:
                logger.error(f"Join teacher error: {e}")
                await update.message.reply_text(f"❌ Error: {str(e)}")

async def how_to_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the How To button - shows interactive help topics."""
    try:
        keyboard = [
            [InlineKeyboardButton("🚀 Getting Started", callback_data="help_getting_started")],
            [InlineKeyboardButton("👨‍🏫 Teacher Guide", callback_data="howto_teacher")],
            [InlineKeyboardButton("👨‍🎓 Student Guide", callback_data="howto_student")],
            [InlineKeyboardButton("❓ Common Issues", callback_data="help_troubleshooting")],
            [InlineKeyboardButton("💡 Tips & Tricks", callback_data="help_tips")],
            [InlineKeyboardButton("💬 Contact Support", url="https://t.me/sardor_ubaydiy")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "📘 **How To Use Spiko**\n\nChoose a topic to get help:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"How To handler error: {e}")
        await update.message.reply_text("❌ Error preparing guide options. Try again later.")


def setup_handlers(application: Application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("switchrole", switch_role_handler))
    application.add_handler(CallbackQueryHandler(how_to_callback, pattern="^howto_"))
    application.add_handler(CommandHandler("generate_token", generate_token_handler))
    application.add_handler(CommandHandler("join_teacher", join_teacher_handler))
    
    application.add_handler(CallbackQueryHandler(role_callback, pattern="^role_"))
    application.add_handler(CallbackQueryHandler(confirm_switch_teacher_callback, pattern="^confirm_switch_teacher$"))
    application.add_handler(CallbackQueryHandler(confirm_switch_student_callback, pattern="^confirm_switch_student$"))
    application.add_handler(CallbackQueryHandler(current_role_info_callback, pattern="^current_role_info$"))
    application.add_handler(CallbackQueryHandler(student_progress_detail_callback, pattern="^prog_stu_"))
    application.add_handler(CallbackQueryHandler(student_tasks_callback, pattern="^student_tasks_"))
    application.add_handler(CallbackQueryHandler(student_analytics_callback, pattern="^student_analytics_"))
    application.add_handler(CallbackQueryHandler(student_compare_callback, pattern="^student_compare_"))
    application.add_handler(CallbackQueryHandler(class_overall_progress_callback, pattern="^prog_class_overall"))
    application.add_handler(CallbackQueryHandler(task_analytics_callback, pattern="^task_ana_"))
    application.add_handler(CallbackQueryHandler(task_start_callback, pattern="^task_start_"))
    application.add_handler(CallbackQueryHandler(task_review_callback, pattern="^task_review_"))
    application.add_handler(CallbackQueryHandler(task_submissions_callback, pattern="^task_subs_"))
    application.add_handler(CallbackQueryHandler(task_refresh_callback, pattern="^task_refresh_"))
    application.add_handler(CallbackQueryHandler(practice_quick_start_callback, pattern="^practice_quick_start$"))
    application.add_handler(CallbackQueryHandler(practice_speaking_start_callback, pattern="^practice_speaking_start$"))
    application.add_handler(CallbackQueryHandler(practice_writing_start_callback, pattern="^practice_writing_start$"))
    application.add_handler(CallbackQueryHandler(practice_history_callback, pattern="^practice_history$"))
    application.add_handler(CallbackQueryHandler(view_score_callback, pattern="^view_score$"))
    application.add_handler(CallbackQueryHandler(next_task_callback, pattern="^next_task$"))
    application.add_handler(CallbackQueryHandler(token_gen_callback, pattern="^token_gen_"))
    application.add_handler(CallbackQueryHandler(token_copy_callback, pattern="^token_copy_"))
    application.add_handler(CallbackQueryHandler(token_share_callback, pattern="^token_share_"))
    application.add_handler(CallbackQueryHandler(purchase_callback, pattern="^purchase_"))
    application.add_handler(CallbackQueryHandler(quick_class_stats_callback, pattern="^quick_class_stats$"))
    application.add_handler(CallbackQueryHandler(quick_student_progress_callback, pattern="^quick_student_progress$"))
    application.add_handler(CallbackQueryHandler(quick_settings_callback, pattern="^quick_settings$"))
    application.add_handler(CallbackQueryHandler(help_getting_started_callback, pattern="^help_getting_started$"))
    application.add_handler(CallbackQueryHandler(help_troubleshooting_callback, pattern="^help_troubleshooting$"))
    application.add_handler(CallbackQueryHandler(help_tips_callback, pattern="^help_tips$"))
    application.add_handler(CallbackQueryHandler(help_menu_callback, pattern="^help_menu$"))
    application.add_handler(CallbackQueryHandler(assignment_started_callback, pattern="^assignment_started_"))
    application.add_handler(CallbackQueryHandler(assignment_remind_callback, pattern="^assignment_remind_"))
    application.add_handler(CallbackQueryHandler(tasks_back_callback, pattern="^tasks_back"))
    application.add_handler(CallbackQueryHandler(progress_back_callback, pattern="^progress_back"))
    
    application.add_handler(MessageHandler(filters.Regex("^📊 Progress$"), progress_handler))
    application.add_handler(MessageHandler(filters.Regex("^📝 Tasks$"), tasks_handler))
    application.add_handler(MessageHandler(filters.Regex("^❓ How To$"), how_to_handler))
    application.add_handler(MessageHandler(filters.Regex("^💳 Buy Credits / Contact Admin$"), buy_credits_handler))
    
    # Handle text messages for token input (must be last to avoid conflicts)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
