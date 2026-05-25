#!/usr/bin/env python3
"""Simulate 10 Jira tasks through chameleon's PreToolUse and PostToolUse hooks.

Tests the full hook pipeline: preflight-and-advise (PreToolUse) injects
pattern context before edits, and posttool-verify (PostToolUse) catches
structural violations after edits.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CHAMELEON_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = CHAMELEON_ROOT / "hooks"
MCP_DIR = CHAMELEON_ROOT / "mcp"
PYTHON = str(MCP_DIR / ".venv" / "bin" / "python")
TS_REPO = "/Users/crisn/Documents/Projects/Testing Apps/ef-client"
RUBY_REPO = "/Users/crisn/Documents/Projects/Testing Apps/ef-api"

TASKS = [
    {
        "jira": "EF-1001",
        "desc": "Add new React component for buyer dashboard widget",
        "tool": "Edit",
        "file": f"{TS_REPO}/src/components/BuyerDashboardWidget.tsx",
        "content": """import React from "react"
import { useUser } from "~/hooks/useUser"

interface Props {
  listingId: string
}

const BuyerDashboardWidget: React.FC<Props> = ({ listingId }) => {
  const { user } = useUser()
  return (
    <div className="buyer-widget">
      <h2>Dashboard for {user.name}</h2>
      <p>Listing: {listingId}</p>
    </div>
  )
}

export default BuyerDashboardWidget
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
    {
        "jira": "EF-1002",
        "desc": "Add new API controller for notifications",
        "tool": "Write",
        "file": f"{RUBY_REPO}/app/controllers/api/v1/notifications_controller.rb",
        "content": """# frozen_string_literal: true

class Api::V1::NotificationsController < Api::V1::BaseController
  before_action :load_notification, only: %i[show update]

  def index
    notifications = current_user.notifications.recent
    render_data(notifications: Serializers::Api::V1::Notification.list(notifications))
  end

  def show
    render_data(notification: Serializers::Api::V1::Notification.show(@notification))
  end

  def update
    if @notification.mark_read!
      render_data(notification: Serializers::Api::V1::Notification.show(@notification))
    else
      render_error(@notification.errors.full_messages, :unprocessable_entity)
    end
  end

  private

  def load_notification
    @notification = current_user.notifications.find(params[:id])
  end
end
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
    {
        "jira": "EF-1003",
        "desc": "Add new model for notification preferences",
        "tool": "Write",
        "file": f"{RUBY_REPO}/app/models/notification_preference.rb",
        "content": """# frozen_string_literal: true

class NotificationPreference < ApplicationRecord
  belongs_to :user, inverse_of: :notification_preferences

  validates :channel, presence: true,
            inclusion: { in: %w[email sms push] }
  validates :enabled, inclusion: { in: [true, false] }

  scope :enabled, -> { where(enabled: true) }
  scope :for_channel, ->(ch) { where(channel: ch) }
end
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
    {
        "jira": "EF-1004",
        "desc": "Add service object for sending notifications",
        "tool": "Write",
        "file": f"{RUBY_REPO}/app/services/notifications/send_notification.rb",
        "content": """# frozen_string_literal: true

module Notifications
  class SendNotification < ActiveInteraction::Base
    object :user, class: User
    string :channel
    string :message

    validates :channel, inclusion: { in: %w[email sms push] }

    def execute
      preference = user.notification_preferences.for_channel(channel).enabled.first
      return unless preference

      case channel
      when 'email'
        NotificationMailer.send_notification(user, message).deliver_later
      when 'sms'
        SmsGateway.send(user.phone, message)
      when 'push'
        PushService.notify(user.device_token, message)
      end
    end
  end
end
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
    {
        "jira": "EF-1005",
        "desc": "Add custom hook for notification bell",
        "tool": "Write",
        "file": f"{TS_REPO}/src/hooks/useNotifications.tsx",
        "content": """import * as React from "react"
import { useQuery } from "@tanstack/react-query"
import { api } from "~/utils/api"

export const useNotifications = () => {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.get("/notifications"),
    refetchInterval: 30000,
  })

  const unreadCount = React.useMemo(
    () => (data?.notifications ?? []).filter((n: any) => !n.read).length,
    [data]
  )

  return { notifications: data?.notifications ?? [], unreadCount, isLoading, refetch }
}
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
    {
        "jira": "EF-1006",
        "desc": "Add type definition for notification entity",
        "tool": "Write",
        "file": f"{TS_REPO}/src/types/Notification.ts",
        "content": """export interface Notification {
  id: string
  userId: string
  channel: "email" | "sms" | "push"
  message: string
  read: boolean
  createdAt: string
}

export interface NotificationPreference {
  id: string
  channel: "email" | "sms" | "push"
  enabled: boolean
}

export type NotificationChannel = Notification["channel"]
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
    {
        "jira": "EF-1007",
        "desc": "Add utility function for formatting notification timestamps",
        "tool": "Write",
        "file": f"{TS_REPO}/src/utils/notificationHelpers.ts",
        "content": """import { formatDistanceToNow } from "date-fns"

export const formatNotificationTime = (createdAt: string): string => {
  return formatDistanceToNow(new Date(createdAt), { addSuffix: true })
}

export const groupNotificationsByDate = (
  notifications: Array<{ createdAt: string }>
) => {
  const groups: Record<string, typeof notifications> = {}
  for (const n of notifications) {
    const day = n.createdAt.slice(0, 10)
    ;(groups[day] ??= []).push(n)
  }
  return groups
}
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
    {
        "jira": "EF-1008",
        "desc": "BAD EDIT: Write a controller that violates the pattern (missing frozen_string_literal, wrong superclass)",
        "tool": "Write",
        "file": f"{RUBY_REPO}/app/controllers/api/v1/bad_controller.rb",
        "content": """class Api::V1::BadController
  def index
    render json: { ok: true }
  end
end
""",
        "expect_archetype": True,
        "expect_violations": True,
    },
    {
        "jira": "EF-1009",
        "desc": "Add new provider for feature flags",
        "tool": "Write",
        "file": f"{TS_REPO}/src/providers/FeatureFlagProvider.tsx",
        "content": """import * as React from "react"

interface FeatureFlags {
  newDashboard: boolean
  notificationBell: boolean
}

const defaultFlags: FeatureFlags = {
  newDashboard: false,
  notificationBell: false,
}

const FeatureFlagContext = React.createContext<FeatureFlags>(defaultFlags)

export const FeatureFlagProvider: React.FC<React.PropsWithChildren> = ({
  children,
}) => {
  const [flags, setFlags] = React.useState<FeatureFlags>(defaultFlags)

  React.useEffect(() => {
    fetch("/api/feature-flags")
      .then((r) => r.json())
      .then(setFlags)
      .catch(() => {})
  }, [])

  return (
    <FeatureFlagContext.Provider value={flags}>
      {children}
    </FeatureFlagContext.Provider>
  )
}

export const useFeatureFlags = () => React.useContext(FeatureFlagContext)
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
    {
        "jira": "EF-1010",
        "desc": "Add spec for notification preference model",
        "tool": "Write",
        "file": f"{RUBY_REPO}/spec/models/notification_preference_spec.rb",
        "content": """# frozen_string_literal: true

require 'rails_helper'

RSpec.describe NotificationPreference, type: :model do
  describe 'associations' do
    it { is_expected.to belong_to(:user).inverse_of(:notification_preferences) }
  end

  describe 'validations' do
    it { is_expected.to validate_presence_of(:channel) }
    it { is_expected.to validate_inclusion_of(:channel).in_array(%w[email sms push]) }
  end

  describe '.enabled' do
    it 'returns only enabled preferences' do
      enabled = create(:notification_preference, enabled: true)
      create(:notification_preference, enabled: false)
      expect(described_class.enabled).to eq([enabled])
    end
  end
end
""",
        "expect_archetype": True,
        "expect_violations": False,
    },
]


def run_hook(hook_name: str, payload: dict, cwd: str) -> dict:
    """Run a chameleon hook script with the given payload on stdin."""
    script = HOOK_DIR / hook_name
    env = {
        **os.environ,
        "CLAUDE_CWD": cwd,
        "CLAUDE_PLUGIN_ROOT": str(CHAMELEON_ROOT),
        "PYTHONPATH": str(MCP_DIR),
    }
    try:
        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        stdout = result.stdout.strip()
        if not stdout:
            return {}
        parsed = json.loads(stdout)
        if "hookSpecificOutput" in parsed:
            return parsed["hookSpecificOutput"]
        return parsed
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        return {"_error": str(e)}


def main():
    results = []
    print("=" * 80)
    print("CHAMELEON HOOK SIMULATION: 10 JIRA TASKS")
    print("=" * 80)

    for i, task in enumerate(TASKS, 1):
        repo = RUBY_REPO if "/ef-api/" in task["file"] else TS_REPO
        print(f"\n{'─' * 80}")
        print(f"Task {i}/10: [{task['jira']}] {task['desc']}")
        print(f"  File: {task['file']}")
        print(f"  Tool: {task['tool']}")

        # --- PreToolUse: preflight-and-advise ---
        pre_payload = {
            "tool_name": task["tool"],
            "tool_input": {"file_path": task["file"]},
            "session_id": f"sim-session-{i}",
        }
        t0 = time.monotonic()
        pre_result = run_hook("preflight-and-advise", pre_payload, repo)
        pre_ms = (time.monotonic() - t0) * 1000

        has_context = bool(pre_result.get("additionalContext"))
        context_text = pre_result.get("additionalContext", "")
        has_archetype = "archetype" in context_text.lower() if context_text else False

        pre_pass = has_context and (has_archetype == task["expect_archetype"])
        print(f"\n  PreToolUse (preflight-and-advise): {pre_ms:.0f}ms")
        print(f"    Has context: {has_context}")
        print(f"    Has archetype info: {has_archetype}")
        if has_archetype:
            # Extract archetype name from context
            for line in context_text.split("\n"):
                if "archetype:" in line.lower() or "Archetype:" in line:
                    print(f"    {line.strip()[:100]}")
                    break
        print(f"    RESULT: {'PASS' if pre_pass else 'FAIL'}")

        # --- PostToolUse: posttool-verify (lint check) ---
        post_payload = {
            "tool_name": task["tool"],
            "tool_input": {"file_path": task["file"]},
            "tool_response": {"content": task["content"], "success": True},
            "session_id": f"sim-session-{i}",
        }
        t0 = time.monotonic()
        post_result = run_hook("posttool-verify", post_payload, repo)
        post_ms = (time.monotonic() - t0) * 1000

        post_context = post_result.get("additionalContext", "")
        has_violations = "violation" in post_context.lower() if post_context else False

        if task["expect_violations"]:
            post_pass = has_violations or bool(post_context)
        else:
            post_pass = not has_violations

        print(f"\n  PostToolUse (posttool-verify): {post_ms:.0f}ms")
        print(f"    Has violations: {has_violations}")
        if post_context:
            # Show first 200 chars of context
            preview = post_context[:200].replace("\n", " ")
            print(f"    Context preview: {preview}...")
        print(f"    Expected violations: {task['expect_violations']}")
        print(f"    RESULT: {'PASS' if post_pass else 'FAIL'}")

        results.append({
            "task": task["jira"],
            "pre_pass": pre_pass,
            "post_pass": post_pass,
            "pre_ms": pre_ms,
            "post_ms": post_ms,
        })

    # Summary
    print(f"\n{'=' * 80}")
    print("SUMMARY")
    print(f"{'=' * 80}")
    pre_passed = sum(1 for r in results if r["pre_pass"])
    post_passed = sum(1 for r in results if r["post_pass"])
    all_passed = sum(1 for r in results if r["pre_pass"] and r["post_pass"])
    avg_pre = sum(r["pre_ms"] for r in results) / len(results)
    avg_post = sum(r["post_ms"] for r in results) / len(results)

    print(f"\n  PreToolUse:  {pre_passed}/10 PASS")
    print(f"  PostToolUse: {post_passed}/10 PASS")
    print(f"  Combined:    {all_passed}/10 PASS")
    print(f"\n  Avg PreToolUse latency:  {avg_pre:.0f}ms")
    print(f"  Avg PostToolUse latency: {avg_post:.0f}ms")

    for r in results:
        status = "PASS" if r["pre_pass"] and r["post_pass"] else "FAIL"
        print(f"  {r['task']}: Pre={'OK' if r['pre_pass'] else 'FAIL'} "
              f"Post={'OK' if r['post_pass'] else 'FAIL'} "
              f"({r['pre_ms']:.0f}ms + {r['post_ms']:.0f}ms) [{status}]")

    sys.exit(0 if all_passed == 10 else 1)


if __name__ == "__main__":
    main()
