import logging
from telegram.ext import ContextTypes
from sqlalchemy import select, func
from bot.db import get_db_session
from bot.models import User, AssessmentScore, SessionUsage

logger = logging.getLogger(__name__)

async def score_handler(update, context: ContextTypes.DEFAULT_TYPE):
    #\"\"Quick score command.\"\"\"
    telegram_user = update.effective_user
    async for session in get_db_session():
        result = await session.execute(select(User).where(User.telegram_id == str(telegram_user.id)))
        user = result.scalars().first()
        if not user:
            await update.message.reply_text("Please /start first to register.")
            return
        
        if user.is_teacher:
            await update.message.reply_text("👨‍🏫 Teachers use /class or 📊 Progress for class stats!")
            return
        
        # Get stats
        avg_query = select(func.avg(AssessmentScore.multilevel_overall_score)).join(SessionUsage).where(SessionUsage.user_id == user.id)
        avg_res = await session.execute(avg_query)
        avg = avg_res.scalar() or 0
        
        latest_query = select(AssessmentScore).join(SessionUsage).where(SessionUsage.user_id == user.id).order_by(AssessmentScore.date.desc()).limit(1)
        latest_res = await session.execute(latest_query)
        latest = latest_res.scalars().first()
        latest_score = latest.multilevel_overall_score if latest else 0
        
        sessions_query = select(func.count(SessionUsage.id)).where(SessionUsage.user_id == user.id)
        sessions_res = await session.execute(sessions_query)
        sessions = sessions_res.scalar()
        
        msg = f'''⭐ **{user.username}'s Scores**

📊 *Avg Score:* **{avg:.1f}/75** 
🎯 *Latest:* **{latest_score:.1f}/75**
⚡ *Sessions:* {sessions}

{'📈 Progressing well!' if avg > 50 else '💪 Keep practicing!'}'''
        
        await update.message.reply_markdown(msg)
