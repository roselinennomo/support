import hashlib

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Company, User
from app.models import AIExecution, utcnow
from app.schemas import (
    AIConsentUpdate,
    LoginRequest,
    PasswordChange,
    RegistrationOut,
    TokenResponse,
    UserCreate,
    UserOut,
)
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentification requise.")
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=["HS256"])
        user_id = payload.get("sub")
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Jeton invalide ou expiré.") from exc
    user = db.get(User, user_id) if user_id else None
    if (
        not user
        or not user.is_active
        or user.account_status != "approved"
        or (user.role == "customer" and not user.company_id)
        or payload.get("ver", 0) != user.token_version
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Compte indisponible.")
    return user


def require_roles(*roles: str):
    def check(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Droits insuffisants.")
        return user

    return check


@router.post("/register", response_model=RegistrationOut, status_code=202)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="Cette adresse e-mail est déjà utilisée.")
    company = db.scalar(
        select(Company).where(
            Company.invitation_code_hash == hashlib.sha256(payload.company_code.encode()).hexdigest(),
            Company.is_active.is_(True),
        )
    )
    if not company:
        raise HTTPException(status_code=400, detail="Code d’entreprise invalide ou entreprise inactive.")
    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role="customer",
        company_id=company.id,
        account_status="pending",
        ai_processing_consent=payload.ai_processing_consent,
        ai_trace_consent=payload.ai_trace_consent,
        ai_consent_updated_at=utcnow(),
    )
    db.add(user)
    db.commit()
    return RegistrationOut(
        status="pending",
        message="Votre demande d’accès a été transmise au support. Vous pourrez vous connecter après validation.",
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="E-mail ou mot de passe incorrect.")
    if user.account_status == "pending":
        raise HTTPException(status_code=403, detail="Votre compte est en attente de validation par le support.")
    if user.account_status == "rejected":
        raise HTTPException(status_code=403, detail="Votre demande d’accès n’a pas été approuvée. Contactez le support.")
    if user.role == "customer" and not user.company_id:
        raise HTTPException(status_code=403, detail="Votre compte doit d’abord être rattaché à une entreprise.")
    return TokenResponse(access_token=create_access_token(user.id, user.token_version), user=user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.put("/password", response_model=TokenResponse)
def change_password(
    payload: PasswordChange,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Le mot de passe actuel est incorrect.")
    user.password_hash = hash_password(payload.new_password)
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return TokenResponse(
        access_token=create_access_token(user.id, user.token_version),
        user=user,
    )


@router.put("/ai-consent", response_model=UserOut)
def update_ai_consent(
    payload: AIConsentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("customer")),
):
    user.ai_processing_consent = payload.ai_processing_consent
    user.ai_trace_consent = payload.ai_trace_consent
    user.ai_consent_updated_at = utcnow()
    if not payload.ai_trace_consent:
        db.query(AIExecution).filter(AIExecution.customer_id == user.id).update(
            {AIExecution.prompt: None, AIExecution.response: None},
            synchronize_session=False,
        )
    db.commit()
    db.refresh(user)
    return user
