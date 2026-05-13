/**
 * Record Fix Command for Raycast
 */

import { Action, ActionPanel, Form, showToast, Toast, popToRoot } from "@raycast/api";
import { useState } from "react";
import { recordFix } from "./api";

export default function Command() {
  const [isLoading, setIsLoading] = useState(false);

  async function handleSubmit(values: { title: string; content: string; tags: string }) {
    if (!values.title.trim()) {
      showToast({ style: Toast.Style.Failure, title: "Title is required" });
      return;
    }

    setIsLoading(true);

    try {
      const tags = values.tags
        .split(",")
        .map((t) => t.trim())
        .filter((t) => t);

      await recordFix(values.title.trim(), values.content.trim(), tags);

      showToast({
        style: Toast.Style.Success,
        title: "🔧 Fix Recorded",
        message: values.title,
      });

      popToRoot();
    } catch (error) {
      showToast({
        style: Toast.Style.Failure,
        title: "Failed to record fix",
        message: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <Form
      isLoading={isLoading}
      actions={
        <ActionPanel>
          <Action.SubmitForm title="Record Fix" onSubmit={handleSubmit} />
        </ActionPanel>
      }
    >
      <Form.TextField
        id="title"
        title="Problem"
        placeholder="What was the problem?"
        autoFocus
      />
      <Form.TextArea
        id="content"
        title="Solution"
        placeholder="What fixed it? (optional)"
      />
      <Form.TextField
        id="tags"
        title="Tags"
        placeholder="comma, separated, tags"
      />
    </Form>
  );
}
