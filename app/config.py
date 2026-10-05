from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    lares_enable_network: bool = False
    lares_allow_remote: bool = False
    lares_enable_schedule: bool = False
    ha_url: str = ''
    ha_token: str = ''
    ollama_url: str = 'http://localhost:11434'
    ollama_model: str = 'qwen3.5:4b'
    ollama_timeout_seconds: int = 900
    model_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    model_context_tokens: int = Field(default=12288, ge=2048)
    model_output_reserve_tokens: int = Field(default=3072, ge=256)
    model_prompt_reserve_tokens: int = Field(default=1000, ge=0)
    distill_context_tokens: int = Field(default=16384, ge=2048)
    remote_payload_token_budget: int = Field(default=32768, ge=2048)
    segments_enabled: bool = True
    segments_ai: bool = False
    segments_ai_mode: str = 'off'
    automatic_distillation: bool = False
    segment_max_events: int = Field(default=400, ge=10)
    segment_max_tokens: int = Field(default=4000, ge=500)
    segment_max_minutes: int = Field(default=60, ge=5)
    segment_min_events: int = Field(default=40, ge=5)
    segment_flush_interval_minutes: int = Field(default=5, ge=1, le=60)
    segment_retry_limit: int = Field(default=1, ge=1, le=20)
    recent_detail_minutes: int = Field(default=60, ge=10, le=240)
    sleep_curtain_entity: str = ''
    sleep_bed_entities: str = ''
    sleep_room: str = 'Example room'
    question_model: str = ''
    question_offpeak_only: bool = False
    question_peak_windows: str = '09:00-12:00,14:00-18:00'
    question_peak_weekdays: str = '1,2,3,4,5'
    arrival_outside_entity: str = ''
    arrival_entry_entity: str = ''
    arrival_entry_door_entity: str = ''
    arrival_pair_window_seconds: int = Field(default=120, ge=10, le=600)
    arrival_short_trip_seconds: int = Field(default=60, ge=0, le=600)
    presence_non_occupancy_entities: str = ''
    presence_noise_entities: str = ''
    home_ai_bind_ip: str = '127.0.0.1'
    home_ai_port: int = 8080
    timezone: str = 'Asia/Shanghai'
    learning_only: bool = True
    learning_days: int = 7
    min_pattern_samples: int = Field(default=5, ge=1, le=100)
    cooccurrence_enabled: bool = True
    database_path: Path = Path('data/home_ai.db')
    config_path: Path = (Path(__file__).parent / 'resources' / 'entities.yaml')
    home_ai_entity_profile_enabled: bool = False
    home_ai_entity_profile_path: Path = (Path(__file__).parent / 'resources' / 'home_ai_entities.proposed.yaml')
    log_path: Path = Path('logs/home-ai.log')
    backup_dir: Path = Path('backups')
    env_window_minutes: int = 15
    presence_debounce_seconds: int = 30
    opening_debounce_seconds: int = Field(default=2, ge=0, le=30)
    correction_window_seconds: int = 180
    web_auth_enabled: bool = False
    web_username: str = ''
    web_password: str = ''
    advisor_api_token: str = ''
    third_party_enabled: bool = False
    third_party_base_url: str = ''
    third_party_api_key: str = ''
    third_party_models: str = ''
    third_party_default_model: str = ''
settings = Settings()
