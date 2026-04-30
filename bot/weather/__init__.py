"""Small weather-market foundation helpers."""

from .market_mapping import WeatherMarketCityMapper, WeatherMarketContext
from .observation_log import ObservationLog
from .historical_provider import HistoricalOpenMeteoWeatherEngine
from .date_matcher import (
    DateMatchValidationResult,
    derive_market_date,
    derive_weather_date,
    validate_weather_date_match,
)
from .replay import (
    DEFAULT_REPLAY_FEE_RATE,
    ReplayFeeModel,
    VALID_REPLAY_ACTIONS,
    WeatherReplayRecord,
    build_weather_replay_dataset,
    build_weather_replay_record,
    iter_weather_replay_records,
    score_replay_answer,
    score_replay_answers,
)
from .registry import RegistryValidationError, WeatherRegistry
from .source_validation import (
    DEFAULT_SOURCE_PILOT_DIR,
    SourceValidationError,
    build_source_validation_report,
    load_source_validation_pilot,
    load_source_validation_pilots,
)
from .station_mapping import (
    STATIC_BASELINE_STATION_MAPPINGS,
    WeatherStationResolution,
    parse_weather_market_city_code,
    resolve_weather_station,
)
from .training import (
    StructuralTrainingPolicy,
    TemperatureTrainingPolicy,
    TemperatureTrainingSample,
    WeatherTrainingExample,
    build_weather_training_examples,
    apply_price_aware_training_updates,
    build_temperature_training_samples,
    load_weather_training_examples_from_history,
    load_temperature_training_samples_from_history,
    run_price_aware_training,
    run_price_aware_training_from_samples,
    run_structural_training,
    run_structural_training_from_examples,
    run_temperature_training,
    run_temperature_training_from_samples,
)

__all__ = [
    "ObservationLog",
    "RegistryValidationError",
    "SourceValidationError",
    "STATIC_BASELINE_STATION_MAPPINGS",
    "DEFAULT_REPLAY_FEE_RATE",
    "DEFAULT_SOURCE_PILOT_DIR",
    "DateMatchValidationResult",
    "HistoricalOpenMeteoWeatherEngine",
    "ReplayFeeModel",
    "StructuralTrainingPolicy",
    "TemperatureTrainingPolicy",
    "TemperatureTrainingSample",
    "VALID_REPLAY_ACTIONS",
    "WeatherReplayRecord",
    "WeatherTrainingExample",
    "WeatherMarketCityMapper",
    "WeatherMarketContext",
    "WeatherRegistry",
    "WeatherStationResolution",
    "apply_price_aware_training_updates",
    "build_source_validation_report",
    "build_weather_training_examples",
    "build_weather_replay_dataset",
    "build_weather_replay_record",
    "build_temperature_training_samples",
    "derive_market_date",
    "derive_weather_date",
    "iter_weather_replay_records",
    "load_source_validation_pilot",
    "load_source_validation_pilots",
    "load_weather_training_examples_from_history",
    "load_temperature_training_samples_from_history",
    "parse_weather_market_city_code",
    "resolve_weather_station",
    "run_price_aware_training",
    "run_price_aware_training_from_samples",
    "run_structural_training",
    "run_structural_training_from_examples",
    "run_temperature_training",
    "run_temperature_training_from_samples",
    "score_replay_answer",
    "score_replay_answers",
    "validate_weather_date_match",
]
