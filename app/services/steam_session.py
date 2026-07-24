from app.core.models import SessionState, SessionStatus


class SteamSessionService:
    """Steam 会话边界。

    当前版本不会收集密码，也不会伪造登录状态。后续由内置官方 Steam
    登录窗口写入系统加密凭据库，再由此服务验证并恢复会话。
    """

    def status(self) -> SessionStatus:
        return SessionStatus(
            state=SessionState.LOGIN_REQUIRED,
            message="尚未接入 Steam 官方登录窗口；当前只能使用演示数据",
        )


steam_session_service = SteamSessionService()

