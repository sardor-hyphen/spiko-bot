import logging
from telegram.ext import ContextTypes
from sqlalchemy import select, func
from bot.db import get_db_session
from bot.models import User, TaskAssignment, AssessmentScore, SessionUsage

async def class_handler(update, context: ContextTypes.DEFAULT_TYPE):
    \"\"\"Teacher class overview.\"\"\"
    telegram_user = update.effective_user
    async for session in get_db_session():
        result = await session.execute(select(User).where(User.telegram_id == str(telegram_user.id)))
        user = result.scalars().first()
        if not user or not user.is_teacher:
            await update.message.reply_text("👨‍🎓 Students use /score. Teachers only!")
            return
        
        # Get students
        students_res = await session.execute(select(User).where(User.assigned_teacher_id == user.id))
        students = students_res.scalars().all()
        if not students:
            await update.message.reply_text("👥 No students assigned yet.\\nUse /generate_token to invite!")
            return
        
        # Class stats
        all_assign_res = await session.execute(
            select(func.count(TaskAssignment.id)).where(TaskAssignment.student_id.in_([s.id for s in students]))
        )
        total_assign = all_assign_res.scalar()
        
        completed_res = await session.execute(
            select(func.count(TaskAssignment.id)).where(
                and_(TaskAssignment.student_id.in_([s.id for s in students]), TaskAssignment.completed == True)
            )
        )
        completed = completed_res.scalar()
        
        # Avg score
        avg_score_res = await session.execute(
            select(func.avg(AssessmentScore.multilevel_overall_score))
            .join(SessionUsage)
            .where(SessionUsage.user_id.in_([s.id for s in students]))
        )
        avg_class = avg_score_res.scalar() or 0
        
        msg = f'''👥 **Class: {user.username}** ({len(students)} students)

📊 *Completion:* **{completed}/{total_assign}** ({(completed/total_assign*100):.0f}%)
⭐ *Class Avg:* **{avg_class:.1f}/75**
📈 *Active:* {len([s for s in students if s.last_login])}

**Top Students:**
'''
        # Top 3 students
        student_stats = []
        for s in students[:5]:
            s_completed = await session.execute(select(func.count(TaskAssignment.id)).where(and_(TaskAssignment.student_id == s.id, TaskAssignment.completed == True)))
            student_stats.append((s, s_completed.scalar()))
        
        student_stats.sort(key=lambda x: x[1], reverse=True)
        for s, comp in student_stats[:3]:
            msg += f"• {s.username}: {comp} tasks\\n"
        
        await update.message.reply_markdown(msg)
