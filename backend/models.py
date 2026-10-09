"""Pydantic models for profile CRUD operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from .webrtc import WebRTCMode, default_mode

BrowserType = Literal["cloakbrowser", "chromium", "firefox", "camoufox"]
FirefoxPreference = bool | int | str


class ProfileCreate(BaseModel):
    name: str
    browser_type: BrowserType = "cloakbrowser"
    webrtc_mode: WebRTCMode = "enabled"
    remote_debugging_enabled: bool = False
    playwright_enabled: bool = False  # Camoufox only
    use_gpu: bool = True  # only used by non-CloakBrowser engines
    keepassxc_enabled: bool | None = None  # null preserves legacy launch-arg behavior
    firefox_preferences: dict[str, FirefoxPreference] = Field(default_factory=dict)
    camoufox_config: dict[str, object] = Field(default_factory=dict)
    fingerprint_seed: int | None = None  # random if not set
    proxy: str | None = None  # "http://user:pass@host:port" or null
    timezone: str | None = None  # "America/New_York"
    locale: str | None = None  # "en-US"
    platform: Literal["windows", "macos", "linux"] = "windows"
    user_agent: str | None = None
    screen_width: int = 1920
    screen_height: int = 1080
    gpu_vendor: str | None = None
    gpu_renderer: str | None = None
    hardware_concurrency: int | None = None
    humanize: bool = False
    human_preset: Literal["default", "careful"] = "default"
    headless: bool = False
    geoip: bool = False
    clipboard_sync: bool = True
    auto_launch: bool = False
    color_scheme: Literal["light", "dark", "no-preference"] | None = None
    launch_args: list[str] = Field(default_factory=list)
    notes: str | None = None
    tags: list[TagCreate] | None = None

    @model_validator(mode="after")
    def browser_defaults(self):
        if self.browser_type == "camoufox" and self.remote_debugging_enabled and self.playwright_enabled:
            raise ValueError("Camoufox BiDi and Playwright endpoints are mutually exclusive")
        if "webrtc_mode" not in self.model_fields_set:
            self.webrtc_mode = default_mode(self.browser_type)
        if self.browser_type != "cloakbrowser" and self.keepassxc_enabled is None:
            self.keepassxc_enabled = True
        if self.browser_type in {"chromium", "firefox"} and "platform" not in self.model_fields_set:
            self.platform = "linux"
        return self


class ProfileUpdate(BaseModel):
    # Reject attempts to change type rather than silently ignoring them.
    @model_validator(mode="before")
    @classmethod
    def immutable_browser_type(cls, values):
        if isinstance(values, dict) and "browser_type" in values:
            raise ValueError("browser_type cannot be changed after profile creation")
        return values

    name: str | None = None
    webrtc_mode: WebRTCMode | None = None
    remote_debugging_enabled: bool | None = None
    playwright_enabled: bool | None = None

    use_gpu: bool | None = None
    keepassxc_enabled: bool | None = None
    firefox_preferences: dict[str, FirefoxPreference] | None = None
    camoufox_config: dict[str, object] | None = None
    fingerprint_seed: int | None = None
    proxy: str | None = Field(default=None)
    timezone: str | None = Field(default=None)
    locale: str | None = Field(default=None)
    platform: Literal["windows", "macos", "linux"] | None = None
    user_agent: str | None = Field(default=None)
    screen_width: int | None = None
    screen_height: int | None = None
    gpu_vendor: str | None = Field(default=None)
    gpu_renderer: str | None = Field(default=None)
    hardware_concurrency: int | None = Field(default=None)
    humanize: bool | None = None
    human_preset: Literal["default", "careful"] | None = None
    headless: bool | None = None
    geoip: bool | None = None
    clipboard_sync: bool | None = None
    auto_launch: bool | None = None
    color_scheme: Literal["light", "dark", "no-preference"] | None = Field(default=None)
    launch_args: list[str] | None = None
    notes: str | None = Field(default=None)
    tags: list[TagCreate] | None = None

    @field_validator("webrtc_mode", "remote_debugging_enabled", "playwright_enabled")
    @classmethod
    def non_null_browser_controls(cls, value):
        if value is None:
            raise ValueError("Browser control settings cannot be null")
        return value


class TagCreate(BaseModel):
    tag: str
    color: str | None = None  # hex color


class TagResponse(BaseModel):
    tag: str
    color: str | None = None


class ProfileResponse(BaseModel):
    id: str
    name: str
    browser_type: BrowserType = "cloakbrowser"
    webrtc_mode: WebRTCMode = "enabled"
    remote_debugging_enabled: bool = False
    playwright_enabled: bool = False
    use_gpu: bool = True
    keepassxc_enabled: bool | None = None
    firefox_preferences: dict[str, FirefoxPreference] = Field(default_factory=dict)
    camoufox_config: dict[str, object] = Field(default_factory=dict)
    fingerprint_seed: int
    proxy: str | None = None
    timezone: str | None = None
    locale: str | None = None
    platform: str = "windows"
    user_agent: str | None = None
    screen_width: int = 1920
    screen_height: int = 1080
    gpu_vendor: str | None = None
    gpu_renderer: str | None = None
    hardware_concurrency: int | None = None
    humanize: bool = False
    human_preset: str = "default"
    headless: bool = False
    geoip: bool = False
    clipboard_sync: bool = True
    auto_launch: bool = False

    @field_validator("clipboard_sync", mode="before")
    @classmethod
    def coerce_clipboard_sync(cls, v: object) -> bool:
        return v if v is not None else True

    color_scheme: str | None = None
    launch_args: list[str] = []
    notes: str | None = None
    user_data_dir: str
    created_at: str
    updated_at: str
    tags: list[TagResponse] = []
    status: str = "stopped"  # "running" | "stopped"
    vnc_ws_port: int | None = None
    cdp_url: str | None = None
    bidi_url: str | None = None
    playwright_url: str | None = None


class LaunchResponse(BaseModel):
    profile_id: str
    status: str = "running"
    vnc_ws_port: int
    display: str
    cdp_url: str | None = None
    bidi_url: str | None = None
    playwright_url: str | None = None


class StatusResponse(BaseModel):
    running_count: int
    binary_version: str
    profiles_total: int


class ProfileStatusResponse(BaseModel):
    status: str  # "running" | "stopped"
    vnc_ws_port: int | None = None
    display: str | None = None
    cdp_url: str | None = None
    bidi_url: str | None = None
    playwright_url: str | None = None


class KeePassXCWindowResponse(BaseModel):
    state: Literal["shown", "minimized"]


class ClipboardRequest(BaseModel):
    text: str = Field(max_length=1_048_576)  # 1MB max


class LoginRequest(BaseModel):
    token: str
