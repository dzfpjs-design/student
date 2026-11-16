"""Synthetic subprocess: force process death after loopback SMTP reply loss."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from home_study_agent.database import Database
from home_study_agent import daily_report_combined_delivery as delivery, daily_report_visual_submission as visual, notification_channels
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
from home_study_agent.report_delivery import ReportDeliveryService

database=Database(Path(sys.argv[1])); service=ReportDeliveryService(database,b's'*32)
telemetry=PilotTestTelemetryService(database,Path(sys.argv[2]))
delivery._source_withdrawal_guard_proven=lambda:True  # isolated synthetic test only
visual._utc_now=lambda:datetime(2026,11,4,4,0,tzinfo=timezone.utc)
notification_channels._admit_external_delivery_budget=lambda:None
delivery._finish=lambda *args:os._exit(91)
delivery.submit(service,telemetry,sys.argv[3])
