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

from bot.config import config
from bot.db import get_db_session, AsyncSessionLocal
from bot.models import User, Task, TaskAssignment, TaskModule, AssessmentScore, SessionUsage
from bot.utils import rate_limit, robust_handler

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

async def get_user_by_telegram_id(session, telegram_id):
    result = await session.execute(select(User).where(User.telegram_id == str(telegram_id)))
    return result.scalars().first()

async def create_or_update_user(session, telegram_user, role=None):
    telegram_id = str(telegram_user.id)
    user = await get_user_by_telegram_id(session, telegram_id)
    
    if not user:
        # Create new user
        # We need a unique username and email
        base_username = telegram_user.username or f"user_{telegram_id}"
        username = base_username
        counter = 1
        
        # Check for existing username
        while True:
            result = await session.execute(select(User).where(User.username == username))
            if not result.scalars().first():
                break
            username = f"{base_username}_{counter}"
            counter += 1
            
        email = f"tg_{telegram_id}@spiko.local" # Placeholder email
        
        user = User(
            username=username,
            email=email,
            telegram_id=telegram_id,
            password_hash=secrets.token_urlsafe(32), 
            auth_provider='telegram',
            is_verified=True,
            is_teacher=(role == 'teacher')
        )
        session.add(user)
    else:
        # Update existing user role if not set or if changing (optional logic)
        # For now, we respect the DB state, but if role is explicitly passed during signup flow
        if role:
            user.is_teacher = (role == 'teacher')
    
    user.last_login = datetime.utcnow()
    await session.commit()
    await session.refresh(user)
    return user

def generate_webapp_url(user: User) -> str:
    """
    Generates the Web App URL with a JWT token that the Backend trusts.
    """
    # 1. Prepare the payload (Must match what your Backend JWT logic expects)
    payload = {
        'user_id': user.id,
        'email': user.email, # Backend might look for this
        'is_teacher': user.is_teacher,
        'is_admin': user.is_admin,
        'exp': datetime.utcnow() + timedelta(days=1), # Long expiry for convenience
        'iat': datetime.utcnow()
    }
    
    # 2. Sign it using the Shared Secret
    token = jwt.encode(payload, config.SECRET_KEY, algorithm='HS256')
    
    # 3. CRITICAL: Send to root "/" NOT "/login"
    # This triggers the automatic redirection logic in App.tsx
    return f"{config.FRONTEND_URL}/?token={token}&provider=telegram"

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
    await query.edit_message_text(text, parse_mode='Markdown')

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
    if update.message:
        await update.message.reply_text(msg, reply_markup=reply_markup)
    elif update.callback_query:
        # If coming from callback, we need to send a new message for ReplyKeyboardMarkup
        await context.bot.send_message(chat_id=user.telegram_id, text=msg, reply_markup=reply_markup)

@robust_handler
@rate_limit
async def switch_role_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows existing users to choose teacher/student again."""
    telegram_user = update.effective_user

    keyboard = [
        [
            InlineKeyboardButton("👨‍🏫 Teacher", callback_data="role_teacher"),
            InlineKeyboardButton("👨‍🎓 Student", callback_data="role_student"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Alright, let’s reset your role. Please choose your new role:",
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
            # Student: Show concise progress card
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
            
            # Next pending module/task
            next_task_result = await session.execute(
                select(Task).join(TaskAssignment).where(
                    and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == False)
                ).order_by(Task.due_date.asc()).limit(1)
            )
            next_task = next_task_result.scalars().first()
            
            msg = f"📊 **Your Progress**\n\n"
            msg += f"Completion: {percentage:.1f}%\n"
            msg += f"Completed Modules: {completed_tasks}/{total_tasks}\n"
            if next_task:
                msg += f"Next Up: {next_task.title} (Due: {next_task.due_date.strftime('%Y-%m-%d')})"
            else:
                msg += "All caught up! 🎉"
                
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
        msg += f"Completion: {percentage:.1f}%\n"
        msg += f"Avg Score: {avg_score:.1f}\n"
        msg += f"Last Active: {student.last_login.strftime('%Y-%m-%d %H:%M') if student.last_login else 'Never'}"
        
        await query.edit_message_text(msg, parse_mode='Markdown')

@robust_handler
@rate_limit
async def class_overall_progress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher views class overall progress."""
    query = update.callback_query
    await query.answer()
    telegram_user = query.from_user
    
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        
        # Get all students
        students_res = await session.execute(select(User).where(User.assigned_teacher_id == user.id))
        students = students_res.scalars().all()
        
        if not students:
            await query.edit_message_text("No students found.")
            return
            
        total_completion_sum = 0
        on_track = 0
        behind = 0
        
        for student in students:
            # Simple metric: > 50% completion is on track (mock logic)
            t_res = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id == student.id))
            t = t_res.scalar() or 0
            c_res = await session.execute(select(func.count(TaskAssignment.id)).where(and_(TaskAssignment.student_id == student.id, TaskAssignment.completed == True)))
            c = c_res.scalar() or 0
            
            p = (c / t * 100) if t > 0 else 0
            total_completion_sum += p
            
            if p >= 50: # Threshold
                on_track += 1
            else:
                behind += 1
                
        class_avg = total_completion_sum / len(students) if students else 0
        
        msg = f"📊 **Class Overall Progress**\n\n"
        msg += f"Class Avg Completion: {class_avg:.1f}%\n"
        msg += f"On Track: {on_track} 🟢\n"
        msg += f"Behind: {behind} 🔴"
        
        await query.edit_message_text(msg, parse_mode='Markdown')


@robust_handler
@rate_limit
async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Tasks button."""
    telegram_user = update.effective_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        
        if user.is_teacher:
            # Teacher: Draft vs Published
            # Simple query for tasks created by teacher
            tasks_res = await session.execute(
                select(Task).where(Task.teacher_id == user.id).order_by(Task.created_at.desc()).limit(10)
            )
            tasks = tasks_res.scalars().all()
            
            msg = "📋 **Assignments**\n\n"
            keyboard = []
            
            for task in tasks:
                status = "✅ Published" if task.is_active else "📝 Draft"
                # Count submissions
                subs_res = await session.execute(select(func.count(TaskAssignment.id)).where(and_(TaskAssignment.task_id == task.id, TaskAssignment.completed == True)))
                subs = subs_res.scalar() or 0
                
                total_res = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.task_id == task.id))
                total = total_res.scalar() or 0
                
                msg += f"• {task.title} ({status})\n   Submissions: {subs}/{total}\n\n"
                
                # Add drill down button
                keyboard.append([InlineKeyboardButton(f"Analyze: {task.title[:15]}...", callback_data=f"task_ana_{task.id}")])
                
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_markdown(msg, reply_markup=reply_markup)
            
        else:
            # Student: Completed vs Pending
            pending_res = await session.execute(
                select(Task).join(TaskAssignment).where(
                    and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == False)
                ).order_by(Task.due_date.asc())
            )
            pending = pending_res.scalars().all()
            
            completed_res = await session.execute(
                select(TaskAssignment).where(
                    and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == True)
                ).order_by(TaskAssignment.completed_at.desc()).options(selectinload(TaskAssignment.task))
            )
            completed_assignments = completed_res.scalars().all()
            
            msg = "📋 **Your Tasks**\n\n"
            msg += "**⏳ Pending:**\n"
            if not pending:
                msg += "None\n"
            for t in pending:
                msg += f"• {t.title} (Due: {t.due_date.strftime('%m-%d')})\n"
                
            msg += "\n**✅ Completed:**\n"
            if not completed_assignments:
                msg += "None\n"
            for a in completed_assignments:
                # Try to get score
                score_res = await session.execute(select(AssessmentScore).where(AssessmentScore.assignment_id == a.id))
                score = score_res.scalars().first()
                score_val = f"{score.overall_score:.1f}" if score else "N/A"
                msg += f"• {a.task.title} (Score: {score_val})\n"
                
            await update.message.reply_markdown(msg)

@robust_handler
@rate_limit
async def task_analytics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher views specific task analytics."""
    query = update.callback_query
    await query.answer()
    
    try:
        task_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid task selection.")
        return

    async for session in get_db_session():
        task = await session.get(Task, task_id)
        if not task:
            await query.edit_message_text("Task not found.")
            return
            
        subs_res = await session.execute(select(func.count(TaskAssignment.id)).where(and_(TaskAssignment.task_id == task.id, TaskAssignment.completed == True)))
        subs = subs_res.scalar() or 0
        total_res = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.task_id == task.id))
        total = total_res.scalar() or 0
        
        msg = f"📊 **Task Analysis: {task.title}**\n\n"
        msg += f"Due Date: {task.due_date.strftime('%Y-%m-%d')}\n"
        msg += f"Submissions: {subs}/{total}\n"
        
        await query.edit_message_text(msg, parse_mode='Markdown')

# --- Token Management Handlers (Teacher & Student) ---

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

async def _call_backend_token_generation(query, user: User, config_data: dict):
    """Call backend API to generate the token."""
    try:
        # Get JWT token for backend authentication
        payload = {
            'user_id': user.id,
            'is_teacher': True,
            'exp': datetime.utcnow() + timedelta(hours=1)
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
            
            await query.edit_message_text(msg, parse_mode='Markdown')
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
                    'exp': datetime.utcnow() + timedelta(hours=1)
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
                    'exp': datetime.utcnow() + timedelta(hours=1)
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

# --- Main Setup ---

@robust_handler
@rate_limit
async def buy_credits_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Opens a link to purchase credits via the admin contact."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "💬 Chat with Admin (@sardor_ubaydiy)",
            url="https://t.me/sardor_ubaydiy"
        )]
    ])
    await update.message.reply_text(
        "💳 *Purchase Credits*\n\n"
        "Credits are 5,000 UZS each.\n\n"
        "📦 Available Packages:\n"
        "• Starter — 5 credits — 25,000 UZS\n"
        "• Standard — 12 credits — 60,000 UZS\n"
        "• Pro — 30 credits — 150,000 UZS\n\n"
        "Contact the admin on Telegram to purchase:",
        parse_mode='Markdown',
        reply_markup=keyboard
    )
async def how_to_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the How To button - shows role selection for user guide."""
    try:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👨‍🏫 Teacher Guide", callback_data="howto_teacher")],
            [InlineKeyboardButton("👨‍🎓 Student Guide", callback_data="howto_student")],
        ])
        await update.message.reply_text(
            "📘 **How To Use Spiko**\n\nChoose your account type to get a tailored walk-through:",
            reply_markup=keyboard,
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
    application.add_handler(CallbackQueryHandler(student_progress_detail_callback, pattern="^prog_stu_"))
    application.add_handler(CallbackQueryHandler(class_overall_progress_callback, pattern="^prog_class_overall"))
    application.add_handler(CallbackQueryHandler(task_analytics_callback, pattern="^task_ana_"))
    application.add_handler(CallbackQueryHandler(token_gen_callback, pattern="^token_gen_"))
    
    application.add_handler(MessageHandler(filters.Regex("^📊 Progress$"), progress_handler))
    application.add_handler(MessageHandler(filters.Regex("^📝 Tasks$"), tasks_handler))
    application.add_handler(MessageHandler(filters.Regex("^❓ How To$"), how_to_handler))
    application.add_handler(MessageHandler(filters.Regex("^💳 Buy Credits / Contact Admin$"), buy_credits_handler))
    
    # Handle text messages for token input (must be last to avoid conflicts)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
