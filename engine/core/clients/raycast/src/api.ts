/**
 * Context DNA API Client for Raycast
 *
 * THIN CLIENT - all logic lives in the server
 */

import { getPreferenceValues } from "@raycast/api";

interface Preferences {
  apiUrl: string;
}

const getApiUrl = () => {
  const { apiUrl } = getPreferenceValues<Preferences>();
  return apiUrl || "http://127.0.0.1:3456";
};

export interface Stats {
  total: number;
  wins: number;
  fixes: number;
  patterns: number;
  today: number;
  streak: number;
  last_updated: string;
}

export interface Learning {
  id: string;
  type: "win" | "fix" | "pattern" | "note";
  title: string;
  content: string;
  tags: string[];
  created_at: string | null;
  score?: number;
}

export async function apiGet<T>(endpoint: string): Promise<T> {
  const response = await fetch(`${getApiUrl()}${endpoint}`);
  if (!response.ok) {
    throw new Error(`API error: ${response.status}`);
  }
  return response.json();
}

export async function apiPost<T>(endpoint: string, body: object): Promise<T> {
  const response = await fetch(`${getApiUrl()}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`API error: ${response.status}`);
  }
  return response.json();
}

export async function getStats(): Promise<Stats> {
  return apiGet<Stats>("/api/stats");
}

export async function getRecent(limit = 10): Promise<{ recent: Learning[] }> {
  return apiGet<{ recent: Learning[] }>(`/api/recent?limit=${limit}`);
}

export async function recordWin(
  title: string,
  content = "",
  tags: string[] = []
): Promise<{ success: boolean; id: string }> {
  return apiPost<{ success: boolean; id: string }>("/api/win", {
    title,
    content,
    tags,
  });
}

export async function recordFix(
  title: string,
  content = "",
  tags: string[] = []
): Promise<{ success: boolean; id: string }> {
  return apiPost<{ success: boolean; id: string }>("/api/fix", {
    title,
    content,
    tags,
  });
}

export async function search(
  query: string,
  limit = 10
): Promise<{ results: Learning[] }> {
  return apiPost<{ results: Learning[] }>("/api/query", { query, limit });
}

export async function consult(task: string): Promise<{ context: string }> {
  return apiPost<{ context: string }>("/api/consult", { task });
}
