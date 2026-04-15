"""
Additional UX Features for Spiko Bot
Contains all the enhanced inline button features for better user experience
"""

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
from bot.handlers import generate_webapp_url, get_user_by_telegram_id

# Enable logging
logger = logging.getLogger(__name__)

# ==============================================================================
# ROLE SWITCHING ENHANCEMENTS
# ==============================================================================

@robust_handler
@rate_limit
async def confirm_switch_teacher_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirms switching to teacher role."""
    query = update.callback_query
    await query.answer()

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user:
            await query.edit_message_text("❌ User not found.")
            return

        if user.is_teacher:
            await query.edit_message_text("✅ You are already a Teacher!")
            return

        # Switch role
        user.is_teacher = True
        await session.commit()

        msg = f"✅ **Role Successfully Changed!**\n\n"
        msg += f"You are now a **Teacher** 👨‍🏫\n\n"
        msg += f"You can now create assignments and manage students."

        keyboard = [[InlineKeyboardButton("🚀 Get Started", callback_data="start")]]
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

        keyboard = [[InlineKeyboardButton("📚 View Tasks", callback_data="start")]]
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

            result = await session.execute(
                select(func.count(TaskAssignment.id)).where(
                    and_(TaskAssignment.student_id == user.id, TaskAssignment.completed == True)
                )
            )
            completed_tasks = result.scalar() or 0

            msg += f"📊 Completion Rate: {completed_tasks}/{total_tasks} ({(completed_tasks/total_tasks*100):.1f}%)\n"
            msg += f"⭐ Average Score: Calculating...\n\n"  # Could calculate this
            msg += f"**Student Features:**\n"
            msg += f"• Complete assignments\n"
            msg += f"• Practice speaking/writing\n"
            msg += f"• View progress analytics\n"
            msg += f"• Receive teacher feedback"

        keyboard = [
            [InlineKeyboardButton("🔄 Switch Role", callback_data="switch_role_quick")],
            [InlineKeyboardButton("⬅️ Back", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

# ==============================================================================
# STUDENT PROGRESS ENHANCEMENTS
# ==============================================================================

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
        msg += f"⭐ Average Score: {avg_score:.1f}/9.0\n"
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
        msg += f"• Student: {student_avg_score:.1f}/9.0\n"
        msg += f"• Class: {class_avg_score:.1f}/9.0\n"
        msg += f"• {'Above' if student_avg_score > class_avg_score else 'Below'} average"

        keyboard = [[InlineKeyboardButton("⬅️ Back to Student", callback_data=f"prog_stu_{student_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

# ==============================================================================
# PRACTICE MODE FEATURES
# ==============================================================================

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
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="start")]
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
        msg += f"⭐ Average Score: {avg_score:.1f}/9.0\n"
        msg += f"⏱️ Practice Time: {(total_sessions * 15) // 60}h {(total_sessions * 15) % 60}m\n\n"

        if total_sessions == 0:
            msg += "Start practicing to see your progress here!"
        else:
            msg += "Keep up the great work! 💪"

        keyboard = [
            [InlineKeyboardButton("🎤 Speaking Practice", callback_data="practice_speaking_start")],
            [InlineKeyboardButton("✍️ Writing Practice", callback_data="practice_writing_start")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

# ==============================================================================
# TASK COMPLETION FEEDBACK
# ==============================================================================

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
        msg += f"⭐ Average Score: {avg_score:.1f}/9.0\n"
        msg += f"🎯 Practice Sessions: {total_sessions}\n"
        msg += f"⏱️ Practice Time: {(total_sessions * 15) // 60}h {(total_sessions * 15) % 60}m\n\n"

        if avg_score >= 7.0:
            msg += "🌟 Excellent work! Keep it up!"
        elif avg_score >= 5.0:
            msg += "👍 Good progress! Keep practicing!"
        else:
            msg += "💪 You're improving! Focus on the feedback!"

        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="start")]]
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

# ==============================================================================
# CREDIT MANAGEMENT
# ==============================================================================

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

# ==============================================================================
# QUICK ACCESS FEATURES
# ==============================================================================

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
        msg += f"⭐ Average Score: {avg_score:.1f}/9.0"

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

# ==============================================================================
# ENHANCED HELP SYSTEM
# ==============================================================================

@robust_handler
@rate_limit
async def help_getting_started_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows getting started guide."""
    query = update.callback_query
    await query.answer()

    msg = "🚀 **Getting Started with Spiko**\n\n"
    msg += "**1️⃣ First Time Setup:**\n"
    msg += "• Click /start to begin\n"
    msg += "• Choose your role (Teacher/Student)\n"
    msg += "• Complete your profile setup\n\n"
    msg += "**2️⃣ For Teachers:**\n"
    msg += "• Generate invite tokens for students\n"
    msg += "• Create assignments in the web app\n"
    msg += "• Monitor student progress\n\n"
    msg += "**3️⃣ For Students:**\n"
    msg += "• Join teacher with invite token\n"
    msg += "• Complete assigned tasks\n"
    msg += "• Practice to improve skills\n\n"
    msg += "**4️⃣ Navigation:**\n"
    msg += "• Use persistent buttons at bottom\n"
    msg += "• Access web app for full features\n"
    msg += "• Check notifications regularly"

    keyboard = [
        [InlineKeyboardButton("👨‍🏫 I'm a Teacher", callback_data="howto_teacher")],
        [InlineKeyboardButton("👨‍🎓 I'm a Student", callback_data="howto_student")],
        [InlineKeyboardButton("⬅️ Back to Help", callback_data="help_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def help_troubleshooting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows troubleshooting guide."""
    query = update.callback_query
    await query.answer()

    msg = "❓ **Common Issues & Solutions**\n\n"
    msg += "**🔐 Login Problems:**\n"
    msg += "• Make sure you've clicked /start\n"
    msg += "• Try the web app login link\n"
    msg += "• Check your internet connection\n\n"
    msg += "**👥 Can't Join Teacher:**\n"
    msg += "• Verify token is correct\n"
    msg += "• Check token hasn't expired\n"
    msg += "• Contact your teacher for new token\n\n"
    msg += "**📝 Assignment Issues:**\n"
    msg += "• Refresh the tasks page\n"
    msg += "• Check due dates\n"
    msg += "• Contact teacher if missing\n\n"
    msg += "**🎤 Practice Problems:**\n"
    msg += "• Allow microphone permissions\n"
    msg += "• Use Chrome/Safari browser\n"
    msg += "• Check audio settings\n\n"
    msg += "**💳 Payment Issues:**\n"
    msg += "• Contact admin for credit top-up\n"
    msg += "• Include your username in message\n"
    msg += "• Credits added within 24 hours"

    keyboard = [[InlineKeyboardButton("⬅️ Back to Help", callback_data="help_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def help_tips_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows tips and tricks."""
    query = update.callback_query
    await query.answer()

    msg = "💡 **Tips & Tricks for Success**\n\n"
    msg += "**🎯 For Students:**\n"
    msg += "• Practice daily for best results\n"
    msg += "• Review AI feedback carefully\n"
    msg += "• Focus on weak areas first\n"
    msg += "• Complete assignments on time\n\n"
    msg += "**👨‍🏫 For Teachers:**\n"
    msg += "• Create clear, focused assignments\n"
    msg += "• Use varied task types\n"
    msg += "• Monitor progress regularly\n"
    msg += "• Provide timely feedback\n\n"
    msg += "**📊 General Tips:**\n"
    msg += "• Use web app for full features\n"
    msg += "• Check notifications regularly\n"
    msg += "• Save important tokens safely\n"
    msg += "• Contact support for help\n\n"
    msg += "**🚀 Pro Tips:**\n"
    msg += "• Speaking: Record in quiet environment\n"
    msg += "• Writing: Read prompts carefully\n"
    msg += "• Practice: Mix different difficulty levels\n"
    msg += "• Progress: Track improvement over time"

    keyboard = [[InlineKeyboardButton("⬅️ Back to Help", callback_data="help_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def help_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Returns to main help menu."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🚀 Getting Started", callback_data="help_getting_started")],
        [InlineKeyboardButton("👨‍🏫 Teacher Guide", callback_data="howto_teacher")],
        [InlineKeyboardButton("👨‍🎓 Student Guide", callback_data="howto_student")],
        [InlineKeyboardButton("❓ Common Issues", callback_data="help_troubleshooting")],
        [InlineKeyboardButton("💡 Tips & Tricks", callback_data="help_tips")],
        [InlineKeyboardButton("💬 Contact Support", url="https://t.me/sardor_ubaydiy")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = "📘 **How To Use Spiko**\n\nChoose a topic to get help:"
    await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')

# ==============================================================================
# NOTIFICATION RESPONSES
# ==============================================================================

@robust_handler
@rate_limit
async def assignment_started_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles when student marks assignment as started."""
    query = update.callback_query
    await query.answer()

    try:
        assignment_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid assignment.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Only students can mark assignments as started.")
            return

        assignment = await session.get(TaskAssignment, assignment_id)
        if not assignment or assignment.student_id != user.id:
            await query.edit_message_text("❌ Assignment not found.")
            return

        if assignment.completed:
            await query.edit_message_text("✅ This assignment is already completed!")
            return

        # Could add a "started_at" timestamp here if needed
        # assignment.started_at = datetime.utcnow()
        # await session.commit()

        msg = f"✅ **Assignment Started!**\n\n"
        msg += f"📝 {assignment.task.title}\n"
        msg += f"📅 Due: {assignment.task.due_date.strftime('%Y-%m-%d')}\n\n"
        msg += f"Good luck! Remember to complete it before the due date."

        keyboard = [
            [InlineKeyboardButton("🌐 Open Task Now", web_app=WebAppInfo(url=generate_webapp_url(user)))]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

@robust_handler
@rate_limit
async def assignment_remind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles assignment reminder request."""
    query = update.callback_query
    await query.answer()

    try:
        assignment_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid assignment.")
        return

    telegram_user = query.from_user
    async for session in get_db_session():
        user = await get_user_by_telegram_id(session, telegram_user.id)
        if not user or user.is_teacher:
            await query.edit_message_text("❌ Only students can set reminders.")
            return

        assignment = await session.get(TaskAssignment, assignment_id)
        if not assignment or assignment.student_id != user.id:
            await query.edit_message_text("❌ Assignment not found.")
            return

        msg = f"⏰ **Reminder Set!**\n\n"
        msg += f"📝 {assignment.task.title}\n"
        msg += f"📅 Due: {assignment.task.due_date.strftime('%Y-%m-%d')}\n\n"
        msg += f"I'll remind you again closer to the due date.\n"
        msg += f"You can always check your tasks in the app."

        keyboard = [[InlineKeyboardButton("📝 View Tasks", callback_data="start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=reply_markup)