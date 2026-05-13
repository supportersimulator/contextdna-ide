#!/usr/bin/env python3
"""
Update NASA Dashboard Google Sheet with 11 Results Fields
Uses Google Sheets API directly
"""

import pickle
import os
from pathlib import Path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from datetime import datetime

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = '19_Zlwg6u7GqeqZ4qIAPDglsfqv6SPRgGS1T0DiVafVI'

def get_credentials():
    """Get or refresh Google credentials."""
    creds = None
    token_path = Path.home() / '.config' / 'gcloud' / 'sheets_token.pickle'
    creds_path = Path.home() / '.config' / 'gcloud' / 'credentials.json'

    # Try to load existing token
    if token_path.exists():
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)

    # If no valid credentials, try to refresh or get new ones
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Try using application default credentials
            try:
                from google.auth import default
                creds, _ = default(scopes=SCOPES)
            except Exception as e:
                print(f"Default credentials not available ({e}), trying local credentials file...")
                if creds_path.exists():
                    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                    creds = flow.run_local_server(port=0)
                else:
                    raise Exception(f"No credentials found. Please place credentials.json at {creds_path}")

        # Save the credentials
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)

    return creds

def add_results_fields():
    """Add 11 results fields to the NASA Dashboard sheet."""
    creds = get_credentials()
    service = build('sheets', 'v4', credentials=creds)

    # Get current sheet info to find last row
    sheet_metadata = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheets = sheet_metadata.get('sheets', [])

    # Find NASA Dashboard sheet or use first sheet
    target_sheet = None
    for sheet in sheets:
        title = sheet['properties']['title']
        if 'NASA' in title or 'Dashboard' in title:
            target_sheet = title
            break
    if not target_sheet:
        target_sheet = sheets[0]['properties']['title']

    print(f"Using sheet: {target_sheet}")

    # Get current data to find last row
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{target_sheet}'!A:A"
    ).execute()
    last_row = len(result.get('values', [])) + 1
    start_row = last_row + 3  # Leave 2 blank rows

    print(f"Adding data starting at row {start_row}")

    # Prepare the data
    timestamp = datetime.now().isoformat()

    headers = [
        'Field ID', 'Display Name', 'Col', 'Backend Status', 'Frontend Status',
        'Aaron Verified', 'Atlas Verified', 'Last Tested', 'Notes', 'humanDoc', 'aiDoc'
    ]

    data_rows = [
        ['score_percent', 'Overall Score %', 'L', '🟢', '🟢', '', '', '', 'Core scoring field',
         'Voice AI sends SCORING_SUMMARY via WebSocket -> full-voice-simulator.tsx:handleScoringSummary() -> completeSession() lib/api/sim.ts:82-129 -> POST /api/sim/sessions/{id}/complete/ -> sim/views.py:679-709 computes scores using ScoringConfig -> stores CaseRun.score_percent (progress/models.py:708) -> SessionCompleteResponse -> NasaResultsOverview:61',
         'trigger:SCORING_SUMMARY_ws -> parse:full-voice-simulator.handleScoringSummary() -> api:POST/api/sim/sessions/{id}/complete/ -> backend:sim/views.py:679-709 -> db:progress.CaseRun.score_percent:708 -> response:outcome.score_percent -> render:NasaResultsOverview:61'],

        ['correct_actions', 'Correct Actions Count', 'M', '🟢', '🟢', '', '', '', 'Clickable cyan button',
         'Voice AI SCORING_SUMMARY.correct_actions[] -> full-voice-simulator.tsx:handleScoringSummary() -> completeSession() lib/api/sim.ts:82-129 -> POST /api/sim/sessions/{id}/complete/ -> sim/views.py:745 stores len() + detail array -> CaseRun.correct_actions (progress/models.py:709) + correct_actions_detail (jsonb) -> SessionCompleteResponse -> NasaResultsOverview:69 -> ActionDetailsModal',
         'trigger:SCORING_SUMMARY.correct_actions[] -> parse:handleScoringSummary() -> api:POST/complete/{correctActionsDetail} -> backend:sim/views.py:745 -> db:CaseRun.correct_actions+detail:709 -> response:outcome.correct_actions -> render:NasaResultsOverview:69 -> modal:ActionDetailsModal'],

        ['wrong_actions', 'Wrong Actions Count', 'N', '🟢', '🟢', '', '', '', 'Red button with severity',
         'Voice AI SCORING_SUMMARY.actions_to_reconsider[] with severity (minor/moderate/critical) -> full-voice-simulator.tsx:handleScoringSummary() -> completeSession() lib/api/sim.ts:82-129 -> sim/views.py:745 stores len() + detail -> penalty_score calc at :695-698 -> CaseRun.wrong_actions (progress/models.py:710) -> NasaResultsOverview:75 red button -> ActionDetailsModal with severity badges',
         'trigger:SCORING_SUMMARY.actions_to_reconsider[{severity}] -> parse:handleScoringSummary() -> api:POST/complete/{actionsToReconsiderDetail} -> backend:sim/views.py:745,695-698 penalty -> db:CaseRun.wrong_actions+detail:710 -> response:outcome.wrong_actions -> render:NasaResultsOverview:75 -> modal:ActionDetailsModal'],

        ['sim_time_seconds', 'Sim Time (MM:SS)', 'O', '🟢', '🟢', '', '', '', 'Frontend timer',
         'Frontend timer starts in handleSessionStart (ai-testing-characters-page:458) -> setInterval increments elapsedSeconds -> completeSession({simTimeSeconds}) -> CaseRun.elapsed_time_seconds -> time_bonus calc if ScoringConfig.time_bonus_enabled -> NasaResultsOverview:83-86 formatted MM:SS',
         'trigger:handleSessionStart:458 -> state:setInterval(elapsedSeconds++) -> api:completeSession({simTimeSeconds}) -> db:CaseRun.elapsed_time_seconds -> scoring:time_bonus_calc -> response:outcome.sim_time_seconds -> render:NasaResultsOverview:83 Math.floor()/padStart'],

        ['objectives', 'Objectives List', 'P', '🟢', '🟢', '', '', '', 'Checklist with icons',
         'Voice AI SCORING_SUMMARY.objectives[{id,description,achieved}] -> definitions in sim/models.py SimCase.learning_objectives -> full-voice-simulator.tsx:handleScoringSummary() -> sim/views.py:686-693 obj_score calc -> CaseRun.objectives_achieved (jsonb) -> NasaResultsOverview:103-119 maps with CheckCircle2/XCircle',
         'trigger:SCORING_SUMMARY.objectives[{achieved:bool}] -> parse:handleScoringSummary() -> api:POST/complete/{objectives} -> backend:sim/views.py:686-693 obj_score -> db:CaseRun.objectives_achieved(jsonb) -> response:outcome.objectives[] -> render:NasaResultsOverview:103-119 CheckCircle2/XCircle'],

        ['correct_actions_detail', 'Correct Actions Detail', 'Q', '🟢', '🟢', '', '', '', 'Array for modal',
         'Full array {id,label,time_offset_sec} from SCORING_SUMMARY.correct_actions[] -> full-voice-simulator.tsx:handleScoringSummary() -> completeSession() lib/api/sim.ts:96-97 -> sim/views.py:745 -> CaseRun.correct_actions_detail (jsonb) -> ActionDetailsModal when cyan button clicked at NasaResultsOverview:169',
         'trigger:SCORING_SUMMARY.correct_actions[{id,label,time_offset_sec}] -> parse:handleScoringSummary() -> api:POST/complete/{correctActionsDetail}:96-97 -> backend:sim/views.py:745 -> db:CaseRun.correct_actions_detail(jsonb) -> response:outcome.correct_actions_detail[] -> modal:action-details-modal.tsx'],

        ['actions_to_reconsider_detail', 'Reconsider Detail', 'R', '🟢', '🟢', '', '', '', 'Array with severity',
         'Full array {id,label,time_offset_sec,severity} from SCORING_SUMMARY.actions_to_reconsider[] -> severity: minor/moderate/critical -> full-voice-simulator.tsx:handleScoringSummary() -> sim/views.py:745,695-698 penalty calc -> CaseRun.actions_to_reconsider_detail (jsonb) -> ActionDetailsModal at NasaResultsOverview:178',
         'trigger:SCORING_SUMMARY.actions_to_reconsider[{severity}] -> parse:handleScoringSummary() -> api:POST/complete/{actionsToReconsiderDetail}:97-98 -> backend:sim/views.py:745,695-698 -> db:CaseRun.actions_to_reconsider_detail(jsonb) -> response:outcome.actions_to_reconsider_detail[] -> modal:action-details-modal.tsx'],

        ['learner_takeaways', 'Your Takeaways', 'S', '🟢', '🟢', '', '', '', 'Bullet list',
         'Voice AI generates 2-4 bullet points via SCORING_SUMMARY.learner_takeaways[] (string array) -> full-voice-simulator.tsx:handleScoringSummary() -> completeSession() lib/api/sim.ts:105-106 -> CaseRun.learner_takeaways (progress/models.py:715 jsonb) -> NasaResultsOverview:152-160 bulleted list',
         'trigger:SCORING_SUMMARY.learner_takeaways[string] -> parse:handleScoringSummary() -> api:POST/complete/{scoringSummary.learner_takeaways}:105-106 -> db:CaseRun.learner_takeaways(jsonb):715 -> response:outcome.learner_takeaways -> render:NasaResultsOverview:152-160 <ul><li>'],

        ['diagnosis_stated', 'Diagnosis Stated', 'T', '🟢', '🟠', '', '', '', 'Not rendered directly',
         'Learner verbal diagnosis via SCORING_SUMMARY.diagnosis.stated (string|null) -> full-voice-simulator.tsx:handleScoringSummary() -> completeSession() lib/api/sim.ts:103-104 -> sim/views.py compares vs SimCase.primary_diagnosis -> CaseRun.diagnosis_stated (progress/models.py:716 text) -> used for comparison, not directly rendered',
         'trigger:SCORING_SUMMARY.diagnosis.stated(string|null) -> parse:handleScoringSummary() -> api:POST/complete/{scoringSummary.diagnosis.stated}:103-104 -> backend:sim/views.py vs SimCase.primary_diagnosis -> db:CaseRun.diagnosis_stated(text):716 -> response:outcome.diagnosis.stated'],

        ['diagnosis_is_correct', 'Diagnosis Correct?', 'U', '🟢', '🟠', '', '', '', 'Used for scoring',
         'Boolean from SCORING_SUMMARY.diagnosis.is_correct (bool|null) -> full-voice-simulator.tsx:handleScoringSummary() -> completeSession() lib/api/sim.ts:103-104 -> sim/views.py:700-705 diag_score calc adds ScoringConfig.diagnosis_weight if true -> CaseRun.diagnosis_is_correct (progress/models.py:717 bool)',
         'trigger:SCORING_SUMMARY.diagnosis.is_correct(bool|null) -> parse:handleScoringSummary() -> api:POST/complete/:103-104 -> backend:sim/views.py:700-705 diag_score -> db:CaseRun.diagnosis_is_correct(bool):717 -> response:outcome.diagnosis.is_correct'],

        ['case_title', 'Case Title', 'V', '🟢', '🟢', '', '', '', 'From case metadata',
         'NOT from SCORING_SUMMARY - from case metadata. sim/models.py SimCase.spark_title + reveal_title -> getCaseDetail() lib/api/sim.ts:29-31 -> GET /api/sim/cases/{id}/ -> sim/views.py:CaseDetailView -> activeCaseId state -> outcome.case_title -> NasaResultsOverview:57 header',
         'source:SimCase.spark_title+reveal_title -> fetch:getCaseDetail():29-31 -> api:GET/api/sim/cases/{id}/ -> backend:CaseDetailView -> state:activeCaseId -> response:outcome.case_title -> render:NasaResultsOverview:57']
    ]

    # Build all values to write
    all_values = [
        [f'RESULTS OVERVIEW PANEL - FIELD TRACKING (11 Fields)'],
        [f'Track field population from Voice AI -> Backend -> Frontend | Added: {timestamp}'],
        [],  # blank row
        headers,
    ] + data_rows + [
        [],  # blank row
        ['STATUS LEGEND:', '🔴 Not Working | 🟡 In Progress | 🟠 Exists Not Wired | 🟢 Working | 🟣 Mock Data'],
        ['VERIFY LEGEND:', '✅ Aaron Verified | 🤖 Atlas/AI Verified | ❌ Failed Verification']
    ]

    # Write the data
    body = {'values': all_values}
    result = service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{target_sheet}'!A{start_row}",
        valueInputOption='RAW',
        body=body
    ).execute()

    print(f"Updated {result.get('updatedCells')} cells")
    print(f"Data written to rows {start_row} - {start_row + len(all_values) - 1}")

    return f"Added 11 results fields starting at row {start_row}"

if __name__ == '__main__':
    result = add_results_fields()
    print(result)
