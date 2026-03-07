import datetime
import uuid
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Float, BigInteger
from sqlalchemy.orm import relationship, backref
from bot.db import Base

# ==============================================================================
# 1. USER & AUTHENTICATION MODELS
# ==============================================================================

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    email = Column(String(120), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_login = Column(DateTime)
    
    # OAuth fields
    google_id = Column(String(100), unique=True, nullable=True)
    telegram_id = Column(String(50), unique=True, nullable=True)
    auth_provider = Column(String(20), default='email')
    is_verified = Column(Boolean, default=False)
    
    is_teacher = Column(Boolean, default=False, nullable=False)
    is_premium = Column(Boolean, default=False, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    
    teacher_token = Column(String(100), unique=True, nullable=True)  
    assigned_teacher_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True) 

    # Relationships
    # Note: We might not need all relationships in the bot, but keeping them for consistency
    # students = relationship('User', backref=backref('teacher', remote_side=[id]), lazy='selectin')

class TaskModule(Base):
    __tablename__ = 'task_modules'

    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    title = Column(String(255), nullable=False)
    task_type = Column(String(50), nullable=False) 
    cefr_level = Column(String(10), default='B2')
    content = Column(JSON, nullable=False) 
    is_public = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Task(Base):
    __tablename__ = 'tasks'

    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    notes = Column(Text, nullable=True) 
    session_id_str = Column(String(100), nullable=False) 
    module_id = Column(Integer, ForeignKey('task_modules.id'), nullable=True)
    due_date = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class TaskAssignment(Base):
    __tablename__ = 'task_assignments'

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('tasks.id'), nullable=False)
    student_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    assigned_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed = Column(Boolean, default=False, nullable=False)
    completed_at = Column(DateTime)
    session_usage_id = Column(Integer, ForeignKey('session_usage.id'), nullable=True)
    feedback_for_teacher = Column(Text, nullable=True)
    
    # Relationships for query convenience
    task = relationship("Task", lazy="selectin")
    # student = relationship("User", foreign_keys=[student_id], lazy="selectin")

class SessionUsage(Base):
    __tablename__ = 'session_usage'
    id = Column(Integer, primary_key=True)
    public_id = Column(String(50), unique=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    session_id_str = Column(String(100), nullable=False)
    duration = Column(Integer, nullable=False)
    words_spoken = Column(Integer)
    date = Column(DateTime, default=datetime.datetime.utcnow)

class AssessmentScore(Base):
    __tablename__ = 'assessment_scores'

    id = Column(Integer, primary_key=True)
    session_usage_id = Column(Integer, ForeignKey('session_usage.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    session_type = Column(String(50), nullable=True) 
    overall_score = Column(Float)
    
    multilevel_overall_score = Column(Float)
    multilevel_fluency_coherence = Column(Float)
    multilevel_lexical_resource = Column(Float)
    multilevel_grammatical_accuracy = Column(Float)
    multilevel_pronunciation = Column(Float)
    
    detailed_analysis = Column(Text) 
    date = Column(DateTime, default=datetime.datetime.utcnow)
    assignment_id = Column(Integer, ForeignKey('task_assignments.id'), nullable=True)

class FeedbackSummary(Base):
    __tablename__ = 'feedback_summary'
    id = Column(Integer, primary_key=True)
    session_usage_id = Column(Integer, ForeignKey('session_usage.id'), nullable=False)
    band_scores = Column(Text, nullable=False) 
    feedback_text = Column(Text) 

class OneTimeToken(Base):
    __tablename__ = 'one_time_tokens'
    id = Column(Integer, primary_key=True)
    token = Column(String(100), unique=True, nullable=False)
    teacher_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    max_uses = Column(Integer, default=1)
    uses_count = Column(Integer, default=0)
    expires_at = Column(DateTime)

class UserSubscription(Base):
    __tablename__ = 'user_subscriptions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, unique=True)
    
    mock_credits = Column(Integer, default=0)
    student_credits = Column(Integer, default=0)
    
    used_free_mock = Column(Boolean, default=False)
    used_free_trial = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime)

class PaymentTransaction(Base):
    __tablename__ = 'payment_transactions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default='USD')
    units = Column(Integer)
    unit_type = Column(String(20))
    
    status = Column(String(20), default='pending')
    payment_method = Column(String(50))
    transaction_id = Column(String(100), unique=True)
    click_trans_id = Column(String(100), nullable=True)
    merchant_prepare_id = Column(String(100), nullable=True)
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime)
    
    bonus_applied = Column(Integer, default=0)
