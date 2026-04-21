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
from bot.shared_utils import generate_webapp_url

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

# Use the shared utility function instead of duplicating code

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
            [InlineKeyboardButton("Quick Class Stats", callback_data="quick_class_stats")],
            [InlineKeyboardButton("Account Settings", callback_data="quick_settings")]
        ]
    else:
        quick_access_keyboard = [
            [InlineKeyboardButton("My Progress", callback_data="quick_student_progress")],
            [InlineKeyboardButton("Account Settings", callback_data="quick_settings")]
        ]

    inline_reply_markup = InlineKeyboardMarkup(quick_access_keyboard) if quick_access_keyboard else None

    if update.message:
        # Always send reply keyboard so persistent buttons appear under text input.
        await update.message.reply_text(msg, reply_markup=reply_markup)
        # Send inline quick actions as a separate message.
        if inline_reply_markup:
            await update.message.reply_text("Quick actions:", reply_markup=inline_reply_markup)
    elif update.callback_query:
        # If coming from callback, send both keyboard types as separate messages.
        await context.bot.send_message(chat_id=user.telegram_id, text=msg, reply_markup=reply_markup)
        if inline_reply_markup:
            await context.bot.send_message(chat_id=user.telegram_id, text="Quick actions:", reply_markup=inline_reply_markup)

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
            f"Role Switching\n\n"
            f"You are currently a {current_role}.\n"
            f"Choose your new role below:",
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
            msg += f"⭐ *Average Score:* {avg_score:.1f}/75\n"
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
        msg += f"⭐ Average Score: {avg_score:.1f}/75\n"
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
        
        # Add inline keyboard with back button
        keyboard = [[InlineKeyboardButton("⬅️ Go Back", callback_data="progress_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)


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

            keyboard = []
            if not pending:
                msg += "None\n"
            else:
                for t in pending:
                    msg += f"• {t.title} (Due: {t.due_date.strftime('%m-%d')})\n"
                    # Add quick action button for each pending task
                    keyboard.append([InlineKeyboardButton(f"▶️ Start: {t.title[:15]}...", callback_data=f"task_start_{t.id}")])

            msg += "\n**✅ Completed:**\n"
            if not completed_assignments:
                msg += "None\n"
            else:
                for a in completed_assignments:
                    # Try to get score
                    score_res = await session.execute(select(AssessmentScore).where(AssessmentScore.assignment_id == a.id))
                    score = score_res.scalars().first()
                    score_val = f"{score.overall_score:.1f}" if score else "N/A"
                    msg += f"• {a.task.title} (Score: {score_val})\n"
                    # Add button to view completed task details
                    keyboard.append([InlineKeyboardButton(f"📊 Review: {a.task.title[:12]}...", callback_data=f"task_review_{a.id}")])

            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            await update.message.reply_markdown(msg, reply_markup=reply_markup)

@robust_handler
@rate_limit
async def task_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Student starts a pending task via inline button."""
    query = update.callback_query
    await query.answer()

    try:
        task_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid task selection.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Only students can start tasks.")
            return

        task = await session.get(Task, task_id)
        if not task:
            await query.edit_message_text("Task not found.")
            return

        # Check if student is assigned to this task
        assignment_res = await session.execute(
            select(TaskAssignment).where(
                and_(TaskAssignment.task_id == task_id, TaskAssignment.student_id == user.id)
            )
        )
        assignment = assignment_res.scalars().first()
        if not assignment:
            await query.edit_message_text("❌ You are not assigned to this task.")
            return

        if assignment.completed:
            await query.edit_message_text("✅ This task is already completed.")
            return

        # Generate WebApp URL and redirect
        webapp_url = generate_webapp_url(user)
        button = InlineKeyboardButton("🌐 Open Task in App", web_app=WebAppInfo(url=webapp_url))
        keyboard = InlineKeyboardMarkup([[button]])

        await query.edit_message_text(
            f"🚀 **Starting Task: {task.title}**\n\n"
            f"📅 Due: {task.due_date.strftime('%Y-%m-%d')}\n\n"
            f"Tap the button below to open the task in the app:",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )

@robust_handler
@rate_limit
async def task_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Student reviews a completed task via inline button."""
    query = update.callback_query
    await query.answer()

    try:
        assignment_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid assignment selection.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Only students can review tasks.")
            return

        assignment = await session.get(TaskAssignment, assignment_id)
        if not assignment or assignment.student_id != user.id:
            await query.edit_message_text("❌ Assignment not found or access denied.")
            return

        if not assignment.completed:
            await query.edit_message_text("❌ This task is not yet completed.")
            return

        # Get score
        score_res = await session.execute(
            select(AssessmentScore).where(AssessmentScore.assignment_id == assignment_id)
        )
        score = score_res.scalars().first()

        msg = f"📊 **Task Review: {assignment.task.title}**\n\n"
        msg += f"📅 Completed: {assignment.completed_at.strftime('%Y-%m-%d %H:%M')}\n"
        if score:
            msg += f"⭐ Score: {score.overall_score:.1f}/75\n"
            msg += f"💬 Feedback: {score.feedback or 'No feedback available'}\n"
        else:
            msg += "⭐ Score: Not yet assessed\n\n"

        msg += f"🎉 **Great job on completing this task!**\n"
        msg += f"What would you like to do next?"

        # Add next action buttons
        keyboard = [
            [InlineKeyboardButton("📈 View My Score", callback_data="view_score")],
            [InlineKeyboardButton("▶️ Start Next Task", callback_data="next_task")],
            [InlineKeyboardButton("🎯 Practice Mode", callback_data="practice_quick_start")],
            [InlineKeyboardButton("📊 Full Analytics", web_app=WebAppInfo(url=generate_webapp_url(user)))]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')

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

        # Add action buttons
        keyboard = [
            [InlineKeyboardButton("👥 View Submissions", callback_data=f"task_subs_{task_id}")],
            [InlineKeyboardButton("🔄 Refresh Stats", callback_data=f"task_refresh_{task_id}")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="tasks_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def task_submissions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher views task submissions."""
    query = update.callback_query
    await query.answer()

    try:
        task_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid task selection.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or not user.is_teacher:
            await query.edit_message_text("❌ Only teachers can view submissions.")
            return

        task = await session.get(Task, task_id)
        if not task or task.teacher_id != user.id:
            await query.edit_message_text("❌ Task not found or access denied.")
            return

        # Get all assignments for this task
        assignments_res = await session.execute(
            select(TaskAssignment).where(TaskAssignment.task_id == task_id).options(selectinload(TaskAssignment.student))
        )
        assignments = assignments_res.scalars().all()

        msg = f"📋 **Submissions for: {task.title}**\n\n"

        completed_count = 0
        for assignment in assignments:
            status = "✅ Completed" if assignment.completed else "⏳ Pending"
            score_text = ""
            if assignment.completed:
                completed_count += 1
                # Get score
                score_res = await session.execute(
                    select(AssessmentScore).where(AssessmentScore.assignment_id == assignment.id)
                )
                score = score_res.scalars().first()
                score_text = f" (Score: {score.overall_score:.1f})" if score else " (Not scored)"

            msg += f"• {assignment.student.username}: {status}{score_text}\n"

        if not assignments:
            msg += "No students assigned yet.\n"

        keyboard = [[InlineKeyboardButton("⬅️ Back to Task", callback_data=f"task_ana_{task_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def task_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher refreshes task statistics."""
    query = update.callback_query
    await query.answer()

    try:
        task_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid task selection.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or not user.is_teacher:
            await query.edit_message_text("❌ Only teachers can refresh stats.")
            return

        task = await session.get(Task, task_id)
        if not task or task.teacher_id != user.id:
            await query.edit_message_text("❌ Task not found or access denied.")
            return

        # Recalculate stats
        subs_res = await session.execute(select(func.count(TaskAssignment.id)).where(and_(TaskAssignment.task_id == task.id, TaskAssignment.completed == True)))
        subs = subs_res.scalar() or 0
        total_res = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.task_id == task.id))
        total = total_res.scalar() or 0

        msg = f"🔄 **Refreshed Task Analysis: {task.title}**\n\n"
        msg += f"Due Date: {task.due_date.strftime('%Y-%m-%d')}\n"
        msg += f"Submissions: {subs}/{total}\n"
        msg += f"_Stats updated at {datetime.utcnow().strftime('%H:%M:%S')}_"

        # Add action buttons
        keyboard = [
            [InlineKeyboardButton("👥 View Submissions", callback_data=f"task_subs_{task_id}")],
            [InlineKeyboardButton("🔄 Refresh Again", callback_data=f"task_refresh_{task_id}")],
            [InlineKeyboardButton("⬅️ Go Back", callback_data="tasks_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def confirm_switch_teacher_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirms switching to teacher role."""
    query = update.callback_query
    logger.info(f"DEBUG: confirm_switch_teacher_callback called with data: {query.data}")
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            logger.error(f"DEBUG: User not found for telegram_id: {telegram_user.id}")
            await query.edit_message_text("❌ User not found.")
            return

        if user.is_teacher:
            logger.info(f"DEBUG: User {user.id} is already a teacher")
            await query.edit_message_text("✅ You are already a Teacher!")
            return

        # Switch role
        user.is_teacher = True
        await session.commit()
        logger.info(f"DEBUG: User {user.id} role switched to teacher")

        msg = f"✅ **Role Successfully Changed!**\n\n"
        msg += f"You are now a **Teacher** 👨‍🏫\n\n"
        msg += f"You can now create assignments and manage students."

        keyboard = [[InlineKeyboardButton("🚀 Get Started", callback_data="menu_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def confirm_switch_student_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirms switching to student role."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await query.edit_message_text("❌ User not found.")
            return

        if not user.is_teacher:
            await query.edit_message_text("✅ You are already a Student!")
            return

        # Switch role
        user.is_teacher = False
        await session.commit()

        msg = f"✅ **Role Successfully Changed!**\n\n"
        msg += f"You are now a **Student** 👨‍🎓\n\n"
        msg += f"You can now receive assignments from teachers."

        keyboard = [[InlineKeyboardButton("📚 View Tasks", callback_data="menu_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def current_role_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows current role information."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await query.edit_message_text("❌ User not found.")
            return

        role = "Teacher" if user.is_teacher else "Student"
        role_emoji = "👨‍🏫" if user.is_teacher else "👨‍🎓"

        msg = f"{role_emoji} **Current Role: {role}**\n\n"

        if user.is_teacher:
            # Teacher info
            result = await session.execute(select(func.count(User.id)).where(User.assigned_teacher_id == user.id))
            student_count = result.scalar() or 0

            result = await session.execute(select(func.count(Task.id)).where(Task.teacher_id == user.id))
            task_count = result.scalar() or 0

            msg += f"👥 Students: {student_count}\n"
            msg += f"📝 Assignments Created: {task_count}\n"
            msg += f"💰 Credits: {user.subscription.mock_credits if hasattr(user, 'subscription') and user.subscription else 0}\n\n"
            msg += f"**Teacher Features:**\n"
            msg += f"• Create and assign tasks\n"
            msg += f"• View student progress\n"
            msg += f"• Generate invite tokens\n"
            msg += f"• Access analytics"
        else:
            # Student info
            result = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id == user.id))
            total_tasks = result.scalar() or 0

            result = await session.execute(select(func.count(TaskAssignment.id)).where(
                and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == True)
            ))
            completed_tasks = result.scalar() or 0

            msg += f"📊 Completion Rate: {completed_tasks}/{total_tasks} tasks\n"
            msg += f"⭐ Average Score: Calculating...\n\n"  # Could calculate this
            msg += f"**Student Features:**\n"
            msg += f"• Complete assignments\n"
            msg += f"• Practice speaking/writing\n"
            msg += f"• View progress analytics\n"
            msg += f"• Receive teacher feedback"

        keyboard = [
            [InlineKeyboardButton("🔄 Switch Role", callback_data="switch_role_quick")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def student_tasks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher views a specific student's tasks."""
    query = update.callback_query
    await query.answer()

    try:
        student_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid student selection.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or not user.is_teacher:
            await query.edit_message_text("❌ Only teachers can view student tasks.")
            return

        student = await session.get(User, student_id)
        if not student or student.assigned_teacher_id != user.id:
            await query.edit_message_text("❌ Student not found or access denied.")
            return

        # Get student's assignments
        assignments_res = await session.execute(
            select(TaskAssignment).where(TaskAssignment.student_id == student_id)
            .options(selectinload(TaskAssignment.task))
            .order_by(TaskAssignment.completed.asc(), Task.due_date.asc())
        )
        assignments = assignments_res.scalars().all()

        msg = f"📋 **{student.username}'s Tasks**\n\n"

        if not assignments:
            msg += "No tasks assigned yet."
        else:
            pending = [a for a in assignments if not a.completed]
            completed = [a for a in assignments if a.completed]

            if pending:
                msg += "**⏳ Pending:**\n"
                for assignment in pending[:5]:  # Show first 5
                    msg += f"• {assignment.task.title} (Due: {assignment.task.due_date.strftime('%m-%d')})\n"
                if len(pending) > 5:
                    msg += f"• ... and {len(pending) - 5} more\n"

            if completed:
                msg += "\n**✅ Completed:**\n"
                for assignment in completed[-3:]:  # Show last 3
                    msg += f"• {assignment.task.title}\n"

        keyboard = [[InlineKeyboardButton("⬅️ Back to Student", callback_data=f"prog_stu_{student_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def student_analytics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher views a specific student's detailed analytics."""
    query = update.callback_query
    await query.answer()

    try:
        student_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid student selection.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or not user.is_teacher:
            await query.edit_message_text("❌ Only teachers can view student analytics.")
            return

        student = await session.get(User, student_id)
        if not student or student.assigned_teacher_id != user.id:
            await query.edit_message_text("❌ Student not found or access denied.")
            return

        # Get detailed analytics
        total_sessions_res = await session.execute(select(func.count(SessionUsage.id)).where(SessionUsage.user_id == student_id))
        total_sessions = total_sessions_res.scalar() or 0

        avg_score_res = await session.execute(
            select(func.avg(AssessmentScore.overall_score))
            .join(SessionUsage, AssessmentScore.session_usage_id == SessionUsage.id)
            .where(SessionUsage.user_id == student_id)
        )
        avg_score = avg_score_res.scalar() or 0.0

        # Get practice time estimate (15 min per session)
        practice_time_hours = (total_sessions * 15) // 60
        practice_time_mins = (total_sessions * 15) % 60

        msg = f"📊 **{student.username}'s Analytics**\n\n"
        msg += f"🎯 Practice Sessions: {total_sessions}\n"
        msg += f"⏱️ Estimated Practice Time: {practice_time_hours}h {practice_time_mins}m\n"
        msg += f"⭐ Average Score: {avg_score:.1f}/75\n"
        msg += f"📅 Member Since: {student.created_at.strftime('%Y-%m-%d') if hasattr(student, 'created_at') and student.created_at else 'Unknown'}"

        keyboard = [[InlineKeyboardButton("⬅️ Back to Student", callback_data=f"prog_stu_{student_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def student_compare_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teacher compares a student with class average."""
    query = update.callback_query
    await query.answer()

    try:
        student_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid student selection.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or not user.is_teacher:
            await query.edit_message_text("❌ Only teachers can compare students.")
            return

        student = await session.get(User, student_id)
        if not student or student.assigned_teacher_id != user.id:
            await query.edit_message_text("❌ Student not found or access denied.")
            return

        # Get student's stats
        student_tasks_res = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id == student_id))
        student_tasks = student_tasks_res.scalar() or 0

        student_completed_res = await session.execute(
            select(func.count(TaskAssignment.id)).where(
                and_(TaskAssignment.student_id == student_id, TaskAssignment.completed == True)
            )
        )
        student_completed = student_completed_res.scalar() or 0

        student_score_res = await session.execute(
            select(func.avg(AssessmentScore.overall_score))
            .join(SessionUsage, AssessmentScore.session_usage_id == SessionUsage.id)
            .where(SessionUsage.user_id == student_id)
        )
        student_avg_score = student_score_res.scalar() or 0.0

        # Get class stats
        class_students_res = await session.execute(select(User.id).where(User.assigned_teacher_id == user.id))
        class_student_ids = [row[0] for row in class_students_res.fetchall()]

        if not class_student_ids:
            await query.edit_message_text("No class data available.")
            return

        # Class completion rate
        class_tasks_res = await session.execute(
            select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id.in_(class_student_ids))
        )
        class_total_tasks = class_tasks_res.scalar() or 0

        class_completed_res = await session.execute(
            select(func.count(TaskAssignment.id)).where(
                and_(TaskAssignment.student_id.in_(class_student_ids), TaskAssignment.completed == True)
            )
        )
        class_completed = class_completed_res.scalar() or 0

        class_score_res = await session.execute(
            select(func.avg(AssessmentScore.overall_score))
            .join(SessionUsage, AssessmentScore.session_usage_id == SessionUsage.id)
            .where(SessionUsage.user_id.in_(class_student_ids))
        )
        class_avg_score = class_score_res.scalar() or 0.0

        # Calculate percentages
        student_completion_pct = (student_completed / student_tasks * 100) if student_tasks > 0 else 0
        class_completion_pct = (class_completed / class_total_tasks * 100) if class_total_tasks > 0 else 0

        msg = f"⚖️ **{student.username} vs Class Average**\n\n"
        msg += f"📊 **Completion Rate:**\n"
        msg += f"• Student: {student_completion_pct:.1f}%\n"
        msg += f"• Class: {class_completion_pct:.1f}%\n"
        msg += f"• {'Above' if student_completion_pct > class_completion_pct else 'Below'} average\n\n"
        msg += f"⭐ **Average Score:**\n"
        msg += f"• Student: {student_avg_score:.1f}/75\n"
        msg += f"• Class: {class_avg_score:.1f}/75\n"
        msg += f"• {'Above' if student_avg_score > class_avg_score else 'Below'} average"

        keyboard = [[InlineKeyboardButton("⬅️ Back to Student", callback_data=f"prog_stu_{student_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def practice_quick_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows quick practice mode selection."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await query.edit_message_text("❌ Please /start first.")
            return

        if user.is_teacher:
            await query.edit_message_text("❌ Practice mode is for students only.")
            return

        msg = f"🎯 **Quick Practice Start**\n\n"
        msg += f"Choose your practice mode below:\n\n"
        msg += f"🎤 **Speaking Practice**\n"
        msg += f"• Improve pronunciation\n"
        msg += f"• Practice conversations\n"
        msg += f"• Get AI feedback\n\n"
        msg += f"✍️ **Writing Practice**\n"
        msg += f"• Enhance writing skills\n"
        msg += f"• Grammar correction\n"
        msg += f"• Style improvement"

        keyboard = [
            [InlineKeyboardButton("🎤 Start Speaking", callback_data="practice_speaking_start")],
            [InlineKeyboardButton("✍️ Start Writing", callback_data="practice_writing_start")],
            [InlineKeyboardButton("📊 Practice History", callback_data="practice_history")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def practice_speaking_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts speaking practice session."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Practice mode is for students only.")
            return

        webapp_url = generate_webapp_url(user)
        button = InlineKeyboardButton("🌐 Open Speaking Practice", web_app=WebAppInfo(url=webapp_url))
        keyboard = InlineKeyboardMarkup([[button]])

        msg = f"🎤 **Starting Speaking Practice**\n\n"
        msg += f"Click the button below to begin your speaking practice session.\n\n"
        msg += f"💡 **Tips:**\n"
        msg += f"• Speak clearly into your microphone\n"
        msg += f"• Complete the prompts for AI feedback\n"
        msg += f"• Practice regularly for best results"

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=keyboard)

@robust_handler
@rate_limit
async def practice_writing_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts writing practice session."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Practice mode is for students only.")
            return

        webapp_url = generate_webapp_url(user)
        button = InlineKeyboardButton("🌐 Open Writing Practice", web_app=WebAppInfo(url=webapp_url))
        keyboard = InlineKeyboardMarkup([[button]])

        msg = f"✍️ **Starting Writing Practice**\n\n"
        msg += f"Click the button below to begin your writing practice session.\n\n"
        msg += f"💡 **Tips:**\n"
        msg += f"• Complete writing prompts\n"
        msg += f"• Review AI grammar corrections\n"
        msg += f"• Focus on different writing styles"

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=keyboard)

@robust_handler
@rate_limit
async def practice_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows practice session history."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Practice history is for students only.")
            return

        # Get practice session count
        sessions_res = await session.execute(select(func.count(SessionUsage.id)).where(SessionUsage.user_id == user.id))
        total_sessions = sessions_res.scalar() or 0

        # Get average score
        avg_score_res = await session.execute(
            select(func.avg(AssessmentScore.overall_score))
            .join(SessionUsage, AssessmentScore.session_usage_id == SessionUsage.id)
            .where(SessionUsage.user_id == user.id)
        )
        avg_score = avg_score_res.scalar() or 0.0

        msg = f"📊 **Your Practice History**\n\n"
        msg += f"🎯 Total Sessions: {total_sessions}\n"
        msg += f"⭐ Average Score: {avg_score:.1f}/75\n"
        msg += f"⏱️ Estimated Practice Time: {(total_sessions * 15) // 60}h {(total_sessions * 15) % 60}m\n\n"

        if total_sessions == 0:
            msg += "Start practicing to see your progress here!"
        else:
            msg += "Keep up the great work! 💪"

        keyboard = [
            [InlineKeyboardButton("🎤 Speaking Practice", callback_data="practice_speaking_start")],
            [InlineKeyboardButton("✍️ Writing Practice", callback_data="practice_writing_start")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def view_score_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows student's overall score summary."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Score viewing is for students only.")
            return

        # Get overall stats
        total_tasks_res = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id == user.id))
        total_tasks = total_tasks_res.scalar() or 0

        completed_res = await session.execute(
            select(func.count(TaskAssignment.id)).where(
                and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == True)
            )
        )
        completed = completed_res.scalar() or 0

        avg_score_res = await session.execute(
            select(func.avg(AssessmentScore.overall_score))
            .join(SessionUsage, AssessmentScore.session_usage_id == SessionUsage.id)
            .where(SessionUsage.user_id == user.id)
        )
        avg_score = avg_score_res.scalar() or 0.0

        sessions_res = await session.execute(select(func.count(SessionUsage.id)).where(SessionUsage.user_id == user.id))
        total_sessions = sessions_res.scalar() or 0

        msg = f"📊 **Your Overall Performance**\n\n"
        msg += f"📝 Task Completion: {completed}/{total_tasks} ({(completed/total_tasks*100):.1f}%)\n"
        msg += f"⭐ Average Score: {avg_score:.1f}/75\n"
        msg += f"🎯 Practice Sessions: {total_sessions}\n"
        msg += f"⏱️ Practice Time: {(total_sessions * 15) // 60}h {(total_sessions * 15) % 60}m\n\n"

        if avg_score >= 7.0:
            msg += "🌟 Excellent work! Keep it up!"
        elif avg_score >= 5.0:
            msg += "👍 Good progress! Keep practicing!"
        else:
            msg += "💪 You're improving! Focus on the feedback!"

        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def next_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Helps student find and start their next task."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Task navigation is for students only.")
            return

        # Find next pending task
        next_task_res = await session.execute(
            select(Task).join(TaskAssignment).where(
                and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == False)
            ).order_by(Task.due_date.asc()).limit(1)
        )
        next_task = next_task_res.scalars().first()

        if next_task:
            # Get assignment
            assignment_res = await session.execute(
                select(TaskAssignment).where(
                    and_(TaskAssignment.task_id == next_task.id, TaskAssignment.student_id == user.id)
                )
            )
            assignment = assignment_res.scalars().first()

            webapp_url = generate_webapp_url(user)
            button = InlineKeyboardButton(f"▶️ Start: {next_task.title[:15]}...", web_app=WebAppInfo(url=webapp_url))
            keyboard = InlineKeyboardMarkup([[button]])

            msg = f"📝 **Your Next Task**\n\n"
            msg += f"**{next_task.title}**\n"
            msg += f"📅 Due: {next_task.due_date.strftime('%Y-%m-%d')}\n\n"
            msg += f"Ready to continue? Click below to start!"

            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=keyboard)
        else:
            msg = f"🎉 **Congratulations!**\n\n"
            msg += f"You've completed all your assigned tasks!\n\n"
            msg += f"Keep practicing to maintain your skills."

            keyboard = [
                [InlineKeyboardButton("🎯 Practice Speaking", callback_data="practice_speaking_start")],
                [InlineKeyboardButton("✍️ Practice Writing", callback_data="practice_writing_start")],
                [InlineKeyboardButton("📊 View Progress", callback_data="quick_student_progress")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

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
            'telegram_id': user.telegram_id,
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
                    'telegram_id': user.telegram_id,
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
                    'telegram_id': user.telegram_id,
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
async def token_copy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles token copy action."""
    query = update.callback_query
    await query.answer()

    try:
        token = query.data.split("_", 2)[2]  # Get everything after "token_copy_"
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid token data.")
        return

    msg = f"📋 **Token Copied!**\n\n"
    msg += f"🔑 `{token}`\n\n"
    msg += f"Send this token to your students so they can join your class."

    await query.edit_message_text(msg, parse_mode='Markdown')

@robust_handler
@rate_limit
async def token_share_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles token share instructions."""
    query = update.callback_query
    await query.answer()

    msg = f"📤 **How to Share Your Token**\n\n"
    msg += f"1️⃣ Copy the token above\n"
    msg += f"2️⃣ Send it to your students via:\n"
    msg += f"   • Telegram message\n"
    msg += f"   • Email\n"
    msg += f"   • Classroom announcement\n\n"
    msg += f"3️⃣ Students use `/join_teacher` command\n"
    msg += f"4️⃣ They paste the token when prompted\n\n"
    msg += f"💡 Tip: Include joining instructions with the token!"

    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="token_back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def quick_class_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows quick class statistics for teachers."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or not user.is_teacher:
            await query.edit_message_text("❌ Only teachers can view class stats.")
            return

        # Get all students
        students_res = await session.execute(select(User).where(User.assigned_teacher_id == user.id))
        students = students_res.scalars().all()

        if not students:
            await query.edit_message_text("📊 **Class Stats**\n\nNo students enrolled yet.")
            return

        total_completion_sum = 0
        on_track = 0
        behind = 0

        for student in students:
            # Simple metric: > 50% completion is on track
            t_res = await session.execute(select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id == student.id))
            t = t_res.scalar() or 0
            c_res = await session.execute(select(func.count(TaskAssignment.id)).where(and_(TaskAssignment.student_id == student.id, TaskAssignment.completed == True)))
            c = c_res.scalar() or 0

            p = (c / t * 100) if t > 0 else 0
            total_completion_sum += p

            if p >= 50:  # Threshold
                on_track += 1
            else:
                behind += 1

        class_avg = total_completion_sum / len(students) if students else 0

        msg = f"📊 **Quick Class Stats**\n\n"
        msg += f"👥 Total Students: {len(students)}\n"
        msg += f"📈 Class Avg Completion: {class_avg:.1f}%\n"
        msg += f"🟢 On Track: {on_track}\n"
        msg += f"🔴 Behind: {behind}"

        keyboard = [[InlineKeyboardButton("📋 View Details", callback_data="prog_class_overall")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def quick_student_progress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows quick student progress."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Only students can view personal progress.")
            return

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

        msg = f"📈 **Your Quick Progress**\n\n"
        msg += f"📊 Completion Rate: {percentage:.1f}%\n"
        msg += f"✅ Tasks Done: {completed_tasks}/{total_tasks}\n"
        msg += f"⭐ Average Score: {avg_score:.1f}/75"

        keyboard = [[InlineKeyboardButton("📋 View Details", callback_data="progress_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def quick_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows quick settings menu."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await query.edit_message_text("❌ User not found.")
            return

        msg = f"⚙️ **Account Settings**\n\n"
        msg += f"👤 Username: {user.username}\n"
        msg += f"📧 Email: {user.email}\n"
        msg += f"🎭 Role: {'Teacher' if user.is_teacher else 'Student'}\n"
        msg += f"📅 Last Login: {user.last_login.strftime('%Y-%m-%d') if user.last_login else 'Never'}"

        keyboard = [
            [InlineKeyboardButton("🔄 Switch Role", callback_data="switch_role_quick")],
            [InlineKeyboardButton("🌐 Open App Settings", web_app=WebAppInfo(url=generate_webapp_url(user)))]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def buy_credits_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows credit purchase options with preset packages."""
    telegram_user = update.effective_user

    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        current_credits = 0
        if user and hasattr(user, 'subscription') and user.subscription:
            current_credits = user.subscription.mock_credits

        msg = "💳 *Purchase Credits*\n\n"
        msg += f"💰 Current Balance: {current_credits} credits\n"
        msg += "💵 Rate: 5,000 UZS per credit\n\n"
        msg += "📦 *Choose Your Package:*\n\n"

        keyboard = [
            [InlineKeyboardButton("💰 Starter: 5 credits (25,000 UZS)", callback_data="purchase_5")],
            [InlineKeyboardButton("💰 Standard: 12 credits (60,000 UZS)", callback_data="purchase_12")],
            [InlineKeyboardButton("💰 Pro: 30 credits (150,000 UZS)", callback_data="purchase_30")],
            [InlineKeyboardButton("💬 Contact Admin (@sardor_ubaydiy)", url="https://t.me/sardor_ubaydiy")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def purchase_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles credit purchase selection."""
    query = update.callback_query
    await query.answer()

    try:
        credits_amount = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid purchase option.")
        return

    # Calculate price (5,000 UZS per credit)
    price = credits_amount * 5000

    msg = f"💰 **Purchase Confirmation**\n\n"
    msg += f"📦 Package: {credits_amount} credits\n"
    msg += f"💵 Price: {price:,} UZS\n"
    msg += f"💡 Rate: 5,000 UZS per credit\n\n"
    msg += f"💬 To complete your purchase, contact the admin with this information:\n\n"
    msg += f"• Username: @{query.from_user.username or 'N/A'}\n"
    msg += f"• Package: {credits_amount} credits ({price:,} UZS)\n"
    msg += f"• Telegram ID: {query.from_user.id}\n\n"
    msg += f"Credits will be added to your account after payment confirmation."

    keyboard = [
        [InlineKeyboardButton("💬 Contact Admin Now", url="https://t.me/sardor_ubaydiy")],
        [InlineKeyboardButton("⬅️ Back to Packages", callback_data="buy_credits")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

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
@rate_limit
async def menu_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the main menu callback."""
    logger.info("menu_main_callback called")
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Main menu accessed!")

@robust_handler
@rate_limit
async def switch_role_quick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick switch role from settings."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await query.edit_message_text("Please /start first.")
            return

        await switch_role_handler(update, context)

@robust_handler
@rate_limit
async def token_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to token generation menu."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or not user.is_teacher:
            await query.edit_message_text("❌ Only teachers can generate tokens.")
            return

        await generate_token_handler(update, context)

@robust_handler
@rate_limit
async def progress_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show progress menu from quick progress."""
    query = update.callback_query
    await query.answer()

    await progress_handler(update, context)

@robust_handler
@rate_limit
async def buy_credits_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show buy credits menu."""
    query = update.callback_query
    await query.answer()

    await buy_credits_handler(update, context)

@robust_handler
@rate_limit
async def help_getting_started_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show getting started help."""
    query = update.callback_query
    await query.answer()

    msg = f"🚀 **Getting Started with Spiko**\n\n"
    msg += f"1️⃣ Start by typing /start in the bot\n"
    msg += f"2️⃣ Choose your role: Teacher or Student\n"
    msg += f"3️⃣ If Teacher: Generate invite token for students\n"
    msg += f"4️⃣ If Student: Join teacher with token\n"
    msg += f"5️⃣ Open the web app to access full features\n\n"
    msg += f"💡 **Pro Tips:**\n"
    msg += f"• Use /switchrole to change roles anytime\n"
    msg += f"• Teachers can create tasks and track progress\n"
    msg += f"• Students can practice and complete assignments"

    keyboard = [[InlineKeyboardButton("⬅️ Back to Help", callback_data="help_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def help_troubleshooting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show troubleshooting help."""
    query = update.callback_query
    await query.answer()

    msg = f"❓ **Common Issues & Solutions**\n\n"
    msg += f"🔐 **Can't access app?**\n"
    msg += f"• Make sure you're logged in via Telegram\n"
    msg += f"• Try /start again to refresh your session\n\n"
    msg += f"👥 **Can't join teacher?**\n"
    msg += f"• Check token spelling and expiry\n"
    msg += f"• Contact your teacher for a new token\n\n"
    msg += f"📝 **Tasks not showing?**\n"
    msg += f"• Refresh the web app\n"
    msg += f"• Check if teacher assigned tasks\n\n"
    msg += f"💰 **Payment issues?**\n"
    msg += f"• Contact admin for credit purchases\n"
    msg += f"• Include your username and amount"

    keyboard = [[InlineKeyboardButton("⬅️ Back to Help", callback_data="help_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def help_tips_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show tips and tricks."""
    query = update.callback_query
    await query.answer()

    msg = f"💡 **Tips & Tricks for Success**\n\n"
    msg += f"🎯 **For Students:**\n"
    msg += f"• Practice regularly to improve scores\n"
    msg += f"• Review AI feedback carefully\n"
    msg += f"• Complete tasks on time\n\n"
    msg += f"👨‍🏫 **For Teachers:**\n"
    msg += f"• Create clear, specific assignments\n"
    msg += f"• Monitor student progress weekly\n"
    msg += f"• Use analytics to identify improvement areas\n\n"
    msg += f"⚡ **General Tips:**\n"
    msg += f"• Use the web app for full functionality\n"
    msg += f"• Keep tokens secure and don't share publicly\n"
    msg += f"• Contact support for technical issues"

    keyboard = [[InlineKeyboardButton("⬅️ Back to Help", callback_data="help_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def help_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help menu."""
    query = update.callback_query
    await query.answer()

    await how_to_handler(update, context)

@robust_handler
@rate_limit
async def all_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch all callback queries for debugging."""
    query = update.callback_query
    logger.info(f"DEBUG: All callback handler triggered with data: {query.data}")
    logger.info(f"DEBUG: User: {query.from_user.id} ({query.from_user.username})")
    await query.answer(f"Debug: {query.data}", show_alert=True)

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
    application.add_handler(CallbackQueryHandler(student_progress_detail_callback, pattern="^prog_stu_"))
    application.add_handler(CallbackQueryHandler(class_overall_progress_callback, pattern="^prog_class_overall"))
    application.add_handler(CallbackQueryHandler(task_analytics_callback, pattern="^task_ana_"))
    application.add_handler(CallbackQueryHandler(task_start_callback, pattern="^task_start_"))
    application.add_handler(CallbackQueryHandler(task_review_callback, pattern="^task_review_"))
    application.add_handler(CallbackQueryHandler(task_submissions_callback, pattern="^task_subs_"))
    application.add_handler(CallbackQueryHandler(task_refresh_callback, pattern="^task_refresh_"))
    application.add_handler(CallbackQueryHandler(token_gen_callback, pattern="^token_gen_"))
    application.add_handler(CallbackQueryHandler(token_copy_callback, pattern="^token_copy_"))
    application.add_handler(CallbackQueryHandler(token_share_callback, pattern="^token_share_"))
    application.add_handler(CallbackQueryHandler(quick_class_stats_callback, pattern="^quick_class_stats$"))
    application.add_handler(CallbackQueryHandler(quick_student_progress_callback, pattern="^quick_student_progress$"))
    application.add_handler(CallbackQueryHandler(quick_settings_callback, pattern="^quick_settings$"))
    application.add_handler(CallbackQueryHandler(tasks_back_callback, pattern="^tasks_back"))
    application.add_handler(CallbackQueryHandler(progress_back_callback, pattern="^progress_back"))
    application.add_handler(CallbackQueryHandler(student_tasks_callback, pattern="^student_tasks_"))
    application.add_handler(CallbackQueryHandler(student_analytics_callback, pattern="^student_analytics_"))
    application.add_handler(CallbackQueryHandler(student_compare_callback, pattern="^student_compare_"))
    application.add_handler(CallbackQueryHandler(confirm_switch_teacher_callback, pattern="^confirm_switch_teacher$"))
    application.add_handler(CallbackQueryHandler(confirm_switch_student_callback, pattern="^confirm_switch_student$"))
    application.add_handler(CallbackQueryHandler(current_role_info_callback, pattern="^current_role_info$"))
    application.add_handler(CallbackQueryHandler(view_score_callback, pattern="^view_score$"))
    application.add_handler(CallbackQueryHandler(next_task_callback, pattern="^next_task$"))
    application.add_handler(CallbackQueryHandler(practice_quick_start_callback, pattern="^practice_quick_start$"))
    application.add_handler(CallbackQueryHandler(practice_speaking_start_callback, pattern="^practice_speaking_start$"))
    application.add_handler(CallbackQueryHandler(practice_writing_start_callback, pattern="^practice_writing_start$"))
    application.add_handler(CallbackQueryHandler(practice_history_callback, pattern="^practice_history$"))
    application.add_handler(CallbackQueryHandler(menu_main_callback, pattern="^menu_main$"))
    application.add_handler(CallbackQueryHandler(switch_role_quick_callback, pattern="^switch_role_quick$"))
    application.add_handler(CallbackQueryHandler(token_share_callback, pattern="^token_share_help$"))
    application.add_handler(CallbackQueryHandler(token_back_callback, pattern="^token_back$"))
    application.add_handler(CallbackQueryHandler(progress_menu_callback, pattern="^progress_menu$"))
    application.add_handler(CallbackQueryHandler(purchase_callback, pattern="^purchase_"))
    application.add_handler(CallbackQueryHandler(buy_credits_callback, pattern="^buy_credits$"))
    application.add_handler(CallbackQueryHandler(help_getting_started_callback, pattern="^help_getting_started$"))
    application.add_handler(CallbackQueryHandler(help_troubleshooting_callback, pattern="^help_troubleshooting$"))
    application.add_handler(CallbackQueryHandler(help_tips_callback, pattern="^help_tips$"))
    application.add_handler(CallbackQueryHandler(help_menu_callback, pattern="^help_menu$"))

    # Catch-all handler for debugging (must be last)
    application.add_handler(CallbackQueryHandler(all_callback_handler, pattern="^.*$"))

    application.add_handler(MessageHandler(filters.Regex("^📊 Progress$"), lambda update, context: progress_handler(update, context)))
    application.add_handler(MessageHandler(filters.Regex("^📝 Tasks$"), lambda update, context: tasks_handler(update, context)))
    application.add_handler(MessageHandler(filters.Regex("^❓ How To$"), lambda update, context: how_to_handler(update, context)))
    application.add_handler(MessageHandler(filters.Regex("^💳 Buy Credits / Contact Admin$"), lambda update, context: buy_credits_handler(update, context)))

    # Handle text messages for token input (must be last to avoid conflicts)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda update, context: message_handler(update, context)))
