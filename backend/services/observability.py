"""OpenTelemetry latency tracing + CloudWatch metrics + Sentry errors.

Measures per turn:
  STT time | LLM time | TTS time | Twilio delay | Total response time
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_initialized = False
_init_lock = threading.Lock()
_tracer = None
_meter = None
_histograms: dict[str, Any] = {}


def setup_observability() -> None:
    """Idempotent startup — OTel, Sentry, CloudWatch hooks."""
    global _initialized, _tracer, _meter
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        from config import get_settings

        settings = get_settings()
        _setup_sentry(settings)
        if getattr(settings, "otel_enabled", True):
            _setup_otel(settings)
        _initialized = True
        logger.info(
            "Observability ready otel=%s sentry=%s cloudwatch=%s",
            bool(_tracer),
            bool((getattr(settings, "sentry_dsn", None) or "").strip()),
            bool(getattr(settings, "cloudwatch_enabled", False)),
        )


def _setup_sentry(settings: Any) -> None:
    dsn = (getattr(settings, "sentry_dsn", None) or "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=(getattr(settings, "sentry_environment", None) or "development"),
            traces_sample_rate=float(getattr(settings, "sentry_traces_sample_rate", 0.2) or 0.2),
            profiles_sample_rate=float(
                getattr(settings, "sentry_profiles_sample_rate", 0.0) or 0.0
            ),
            integrations=[
                FastApiIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
            send_default_pii=False,
        )
        logger.info("Sentry initialized env=%s", getattr(settings, "sentry_environment", ""))
    except Exception:
        logger.exception("Sentry init failed")


def _setup_otel(settings: Any) -> None:
    global _tracer, _meter, _histograms
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        service = (getattr(settings, "otel_service_name", None) or "call-agent").strip()
        resource = Resource.create(
            {
                "service.name": service,
                "service.version": "2.1.0",
                "deployment.environment": getattr(settings, "sentry_environment", "development")
                or "development",
            }
        )

        provider = TracerProvider(resource=resource)
        endpoint = (getattr(settings, "otel_exporter_otlp_endpoint", None) or "").strip()
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            except Exception:
                logger.exception("OTLP trace exporter failed — using console")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif getattr(settings, "otel_console_export", False):
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("call_agent.latency")

        # Metrics — optional OTLP
        readers = []
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )

                readers.append(
                    PeriodicExportingMetricReader(
                        OTLPMetricExporter(endpoint=endpoint.replace("/v1/traces", "/v1/metrics")
                        if endpoint.endswith("/v1/traces")
                        else endpoint),
                        export_interval_millis=15000,
                    )
                )
            except Exception:
                logger.warning("OTLP metric exporter unavailable", exc_info=True)

        if readers:
            metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
        else:
            metrics.set_meter_provider(MeterProvider(resource=resource))

        _meter = metrics.get_meter("call_agent.latency")
        for name in ("stt_ms", "llm_ms", "tts_ms", "twilio_delay_ms", "total_response_ms"):
            _histograms[name] = _meter.create_histogram(
                name=f"call_agent.{name}",
                unit="ms",
                description=f"Voice turn {name}",
            )
    except Exception:
        logger.exception("OpenTelemetry init failed — latency still logged locally")
        _tracer = None
        _meter = None


def record_turn_latency(
    *,
    call_id: str,
    path: str,
    metrics: dict[str, Any],
) -> None:
    """Emit structured latency log + OTel span/metrics + CloudWatch + Sentry breadcrumb."""
    payload = {
        "event": "turn_latency",
        "call_id": call_id,
        "path": path,
        "stt_ms": float(metrics.get("stt_ms") or 0),
        "llm_ms": float(metrics.get("llm_ms") or 0),
        "tts_ms": float(metrics.get("tts_ms") or 0),
        "twilio_delay_ms": float(metrics.get("twilio_delay_ms") or 0),
        "total_response_ms": float(
            metrics.get("total_response_ms") or metrics.get("handler_ms") or 0
        ),
        "first_audio_ms": metrics.get("first_audio_ms"),
        "turn_id": metrics.get("turn_id"),
    }
    logger.info(
        "LATENCY call=%s path=%s stt=%.0fms llm=%.0fms tts=%.0fms twilio=%.0fms total=%.0fms first_audio=%s",
        call_id,
        path,
        payload["stt_ms"],
        payload["llm_ms"],
        payload["tts_ms"],
        payload["twilio_delay_ms"],
        payload["total_response_ms"],
        payload.get("first_audio_ms"),
    )
    # Machine-parseable line for CloudWatch Logs metric filters / Insights
    logger.info("LATENCY_JSON %s", json.dumps(payload, separators=(",", ":")))

    _otel_record(payload)
    _cloudwatch_record(payload)
    _sentry_breadcrumb(payload)


def _otel_record(payload: dict[str, Any]) -> None:
    if _tracer is not None:
        try:
            from opentelemetry import trace
            from opentelemetry.trace import Status, StatusCode

            with _tracer.start_as_current_span("voice.turn") as span:
                span.set_attribute("call.id", payload["call_id"])
                span.set_attribute("turn.path", payload["path"])
                for key in ("stt_ms", "llm_ms", "tts_ms", "twilio_delay_ms", "total_response_ms"):
                    span.set_attribute(f"latency.{key}", payload[key])
                if payload.get("first_audio_ms") is not None:
                    span.set_attribute("latency.first_audio_ms", float(payload["first_audio_ms"]))
                span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.debug("OTel span failed", exc_info=True)

    attrs = {"path": payload["path"]}
    for key, hist in _histograms.items():
        try:
            hist.record(payload.get(key) or 0, attrs)
        except Exception:
            pass


def _cloudwatch_record(payload: dict[str, Any]) -> None:
    try:
        from config import get_settings

        settings = get_settings()
        if not getattr(settings, "cloudwatch_enabled", False):
            return
        import boto3

        region = (getattr(settings, "aws_region", None) or "us-east-1").strip()
        namespace = (getattr(settings, "cloudwatch_namespace", None) or "CallAgent/Latency").strip()
        client = boto3.client("cloudwatch", region_name=region)
        dims = [
            {"Name": "Path", "Value": str(payload.get("path") or "unknown")[:256]},
            {"Name": "Service", "Value": (getattr(settings, "otel_service_name", None) or "call-agent")},
        ]
        metric_data = []
        for name, key in (
            ("STTTime", "stt_ms"),
            ("LLMTime", "llm_ms"),
            ("TTSTime", "tts_ms"),
            ("TwilioDelay", "twilio_delay_ms"),
            ("TotalResponseTime", "total_response_ms"),
        ):
            metric_data.append(
                {
                    "MetricName": name,
                    "Dimensions": dims,
                    "Value": float(payload.get(key) or 0),
                    "Unit": "Milliseconds",
                }
            )
        if payload.get("first_audio_ms") is not None:
            metric_data.append(
                {
                    "MetricName": "FirstAudioTime",
                    "Dimensions": dims,
                    "Value": float(payload["first_audio_ms"]),
                    "Unit": "Milliseconds",
                }
            )
        client.put_metric_data(Namespace=namespace, MetricData=metric_data)
    except Exception:
        logger.debug("CloudWatch put_metric_data failed", exc_info=True)


def _sentry_breadcrumb(payload: dict[str, Any]) -> None:
    try:
        import sentry_sdk

        sentry_sdk.add_breadcrumb(
            category="latency",
            message="voice.turn",
            level="info",
            data=payload,
        )
        # Flag slow turns as Sentry messages (not errors) when over threshold
        from config import get_settings

        threshold = float(getattr(get_settings(), "latency_slow_total_ms", 3000) or 3000)
        total = float(payload.get("total_response_ms") or 0)
        if total >= threshold:
            with sentry_sdk.push_scope() as scope:
                scope.set_context("latency", payload)
                scope.set_tag("call_id", payload.get("call_id"))
                scope.set_tag("path", payload.get("path"))
                sentry_sdk.capture_message(
                    f"Slow voice turn {total:.0f}ms (path={payload.get('path')})",
                    level="warning",
                )
    except Exception:
        pass


def capture_exception(exc: BaseException, **context: Any) -> None:
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for k, v in context.items():
                scope.set_extra(k, v)
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass
