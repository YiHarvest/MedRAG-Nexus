"use client";

import { Renew, TrashCan } from "@carbon/icons-react";
import { Button, InlineNotification } from "@carbon/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { type AdminAccount, listWebUiAccounts } from "@/lib/webui-admin-api";
import {
  describeWebUiError,
  getWebUiPolicyBindings,
  type PermissionGroupDefinition,
  type PolicyBinding,
  type PolicyBindingAction,
  type PolicyPrincipalType,
  type PolicyResourceType,
  updateWebUiPolicyBindings,
} from "@/lib/webui-api";
import styles from "./policy-bindings-editor.module.css";

export interface PolicyActionOption {
  action: PolicyBindingAction;
  label: string;
  description: string;
  pluginId?: string;
  available?: boolean;
}

interface PolicyBindingsEditorProps {
  resourceType: PolicyResourceType;
  resourceId: string;
  actions: PolicyActionOption[];
  accounts?: AdminAccount[];
  groups?: PermissionGroupDefinition[];
  canListAccounts?: boolean;
  disabled?: boolean;
  onSaved?: () => void;
}

interface AudienceOption {
  type: PolicyPrincipalType;
  id: string;
  label: string;
  detail: string;
  permissions: ReadonlySet<string> | null;
}

function initialSelectedActions(actions: PolicyActionOption[]): Set<PolicyBindingAction> {
  const first = actions.find((item) => item.available !== false) ?? actions[0];
  return new Set(first ? [first.action] : []);
}

function permissionGroupLabel(key: string, groups: PermissionGroupDefinition[]): string {
  const group = groups.find((item) => item.key === key);
  if (group?.name && !group.name.startsWith("webui.")) return group.name;
  return group?.description ?? "自定义权限组";
}

export function PolicyBindingsEditor({
  resourceType,
  resourceId,
  actions,
  accounts = [],
  groups = [],
  canListAccounts = false,
  disabled = false,
  onSaved,
}: PolicyBindingsEditorProps) {
  const [selectedActions, setSelectedActions] = useState<Set<PolicyBindingAction>>(
    () => initialSelectedActions(actions),
  );
  const [selectedAudience, setSelectedAudience] = useState<PolicyPrincipalType>("account");
  const [bindings, setBindings] = useState<Partial<Record<PolicyBindingAction, PolicyBinding[]>>>({});
  const [dirtyActions, setDirtyActions] = useState<Set<PolicyBindingAction>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [loadedAccounts, setLoadedAccounts] = useState<AdminAccount[]>([]);

  const load = useCallback(async () => {
    if (!resourceId) return;
    setLoading(true);
    setError(null);
    try {
      const response = await getWebUiPolicyBindings(resourceType, resourceId);
      setBindings(response.bindings);
      setDirtyActions(new Set());
    } catch (caught) {
      setError(describeWebUiError(caught));
    } finally {
      setLoading(false);
    }
  }, [resourceId, resourceType]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    if (!canListAccounts || accounts.length) return;
    let cancelled = false;
    void listWebUiAccounts()
      .then((response) => {
        if (!cancelled) setLoadedAccounts(response.accounts);
      })
      .catch(() => {
        // 用户列表不可见时仍可通过权限组完成授权。
      });
    return () => { cancelled = true; };
  }, [accounts.length, canListAccounts]);

  const selectableAccounts = accounts.length ? accounts : loadedAccounts;
  const selectedActionOptions = actions.filter((item) => selectedActions.has(item.action));
  const allBindings = useMemo(() => Object.values(bindings).flatMap((items) => items ?? []), [bindings]);

  const accountOptions = useMemo<AudienceOption[]>(() => {
    const known = selectableAccounts.filter((item) => item.permission_level !== 1000).map((item) => ({
      type: "account" as const,
      id: item.account_id,
      label: item.display_name,
      detail: `登录名：${item.login_name}`,
      permissions: new Set(item.permissions ?? []),
    }));
    const knownIds = new Set(known.map((item) => item.id));
    const retained = allBindings
      .filter((item) => item.principal_type === "account" && item.managed_by !== "system.superadmin" && !knownIds.has(item.principal_id))
      .map((item) => ({
        type: "account" as const,
        id: item.principal_id,
        label: "已授权用户",
        detail: "用户资料当前不可见",
        permissions: null,
      }));
    return [...known, ...retained.filter((item, index, values) => values.findIndex((value) => value.id === item.id) === index)];
  }, [allBindings, selectableAccounts]);

  const groupOptions = useMemo<AudienceOption[]>(() => {
    const keys = Array.from(new Set([
      ...groups.map((group) => group.key),
      ...selectableAccounts.flatMap((item) => item.groups),
      ...allBindings.filter((item) => item.principal_type === "group").map((item) => item.principal_id),
    ])).sort((left, right) => left.localeCompare(right));
    return keys.map((key) => ({
      type: "group" as const,
      id: key,
      label: permissionGroupLabel(key, groups),
      detail: `${selectableAccounts.filter((item) => item.groups.includes(key)).length} 位成员`,
      permissions: new Set(groups.find((group) => group.key === key)?.permissions ?? []),
    }));
  }, [allBindings, groups, selectableAccounts]);

  const audienceOptions = selectedAudience === "account" ? accountOptions : groupOptions;
  const deniedBindings = selectedActionOptions.flatMap((action) =>
    (bindings[action.action] ?? [])
      .filter((item) => item.effect === "deny")
      .map((binding) => ({ action, binding })),
  );

  function principalLabel(item: PolicyBinding): string {
    if (item.principal_type === "account") {
      return accountOptions.find((option) => option.id === item.principal_id)?.label ?? "已授权用户";
    }
    return permissionGroupLabel(item.principal_id, groups);
  }

  function markDirty(changedActions: Iterable<PolicyBindingAction>) {
    setDirtyActions((current) => {
      const next = new Set(current);
      for (const action of changedActions) next.add(action);
      return next;
    });
    setSuccess(null);
  }

  function toggleAction(action: PolicyBindingAction) {
    if (selectedActions.has(action) && selectedActions.size === 1) {
      setError("至少选择一项权限。");
      return;
    }
    const next = new Set(selectedActions);
    if (next.has(action)) next.delete(action);
    else next.add(action);
    setSelectedActions(next);
    setError(null);
    setSuccess(null);
  }

  function toggleAllow(option: AudienceOption) {
    const matchingByAction = selectedActionOptions.map(({ action }) => ({
      action,
      matching: (bindings[action] ?? []).filter(
        (item) => item.principal_type === option.type && item.principal_id === option.id,
      ),
    }));
    if (matchingByAction.some(({ matching }) => matching.some((item) => item.immutable))) {
      setError("该授权由系统保护，不能修改。");
      return;
    }
    const allAllowed = matchingByAction.every(({ matching }) => matching.some((item) => item.effect === "allow"));
    const missingActions = selectedActionOptions.filter(
      ({ action }) => option.permissions === null || !option.permissions.has(action),
    );
    if (!allAllowed && missingActions.length) {
      setError(
        `请先为${option.type === "account" ? "该用户" : "该权限组"}开启“${missingActions.map((item) => item.label).join("、")}”权限。`,
      );
      return;
    }
    setBindings((current) => {
      const next = { ...current };
      for (const { action } of selectedActionOptions) {
        const retained = (current[action] ?? []).filter((item) => !(
          item.principal_type === option.type &&
          item.principal_id === option.id &&
          (!allAllowed || item.effect === "allow")
        ));
        if (!allAllowed) retained.push({ principal_type: option.type, principal_id: option.id, effect: "allow" });
        next[action] = retained;
      }
      return next;
    });
    setError(null);
    markDirty(selectedActions);
  }

  function removeBinding(action: PolicyBindingAction, item: PolicyBinding) {
    if (item.immutable) return;
    setBindings((current) => ({
      ...current,
      [action]: (current[action] ?? []).filter((candidate) => !(
        candidate.principal_type === item.principal_type &&
        candidate.principal_id === item.principal_id &&
        candidate.effect === item.effect
      )),
    }));
    markDirty([action]);
  }

  async function saveAll() {
    const targets = actions.filter((item) => dirtyActions.has(item.action));
    if (!targets.length) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      let latest = bindings;
      for (const target of targets) {
        const draft = bindings[target.action] ?? [];
        const response = await updateWebUiPolicyBindings(resourceType, resourceId, {
          action: target.action,
          bindings: draft.filter((item) => !item.immutable).map((item) => ({
            principal_type: item.principal_type,
            principal_id: item.principal_id,
            effect: item.effect,
          })),
        });
        latest = response.bindings;
      }
      setBindings(latest);
      setDirtyActions(new Set());
      setSuccess(`已保存 ${targets.length} 项操作的访问范围。`);
      onSaved?.();
    } catch (caught) {
      setError(describeWebUiError(caught));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className={styles.editor} data-policy-bindings-editor aria-label="访问范围设置">
      <div className={styles.heading}>
        <span>访问名单</span>
        <h4>谁可以使用这里的内容？</h4>
        <p>先选择一项或多项权限，再勾选允许执行这些操作的用户或权限组。至少选择一项权限，未勾选的对象默认不能访问。</p>
      </div>

      <div className={styles.superadminNotice}><strong>超级管理员自动拥有全部管理权限</strong><span>系统会自动为所有超级管理员建立资源授权，无需在每个知识域或知识库中重复勾选。</span></div>

      {error ? <InlineNotification lowContrast kind="error" title="设置失败" subtitle={error} onCloseButtonClick={() => setError(null)} /> : null}
      {success ? <InlineNotification lowContrast kind="success" title="访问范围已更新" subtitle={success} onCloseButtonClick={() => setSuccess(null)} /> : null}

      <div className={styles.actionPicker} role="group" aria-label="选择一项或多项要设置的权限">
        {actions.map((item) => {
          const allowedCount = (bindings[item.action] ?? []).filter((binding) => binding.effect === "allow" && binding.managed_by !== "system.superadmin").length;
          return <button
            type="button"
            aria-pressed={selectedActions.has(item.action)}
            data-selected={selectedActions.has(item.action)}
            key={item.action}
            disabled={disabled || loading || saving || item.available === false}
            onClick={() => toggleAction(item.action)}
          >
            <strong>{item.label}</strong>
            <small>{item.available === false ? "当前不可用" : `${allowedCount} 个对象`}</small>
          </button>;
        })}
      </div>

      <div className={styles.currentAction}>
        <div>
          <span>批量设置</span>
          <h5>{selectedActionOptions.length === 1 ? selectedActionOptions[0]?.label : `已选择 ${selectedActionOptions.length} 项权限`}</h5>
          <p>{selectedActionOptions.length === 1
            ? selectedActionOptions[0]?.description
            : `将同时设置：${selectedActionOptions.map((item) => item.label).join("、")}`}</p>
        </div>
        <div className={styles.audienceSwitch} role="tablist" aria-label="选择用户或权限组">
          <button type="button" role="tab" aria-selected={selectedAudience === "account"} data-selected={selectedAudience === "account"} onClick={() => setSelectedAudience("account")}>用户</button>
          <button type="button" role="tab" aria-selected={selectedAudience === "group"} data-selected={selectedAudience === "group"} onClick={() => setSelectedAudience("group")}>权限组</button>
        </div>
      </div>

      {loading ? <p className={styles.empty}>正在读取访问名单…</p> : audienceOptions.length ? (
        <div className={styles.audienceGrid}>
          {audienceOptions.map((option) => {
            const matchingByAction = selectedActionOptions.map(({ action }) =>
              (bindings[action] ?? []).filter((item) => item.principal_type === option.type && item.principal_id === option.id),
            );
            const allowedCount = matchingByAction.filter((matching) => matching.some((item) => item.effect === "allow")).length;
            const allowed = selectedActionOptions.length > 0 && allowedCount === selectedActionOptions.length;
            const partiallyAllowed = allowedCount > 0 && !allowed;
            const denied = matchingByAction.some((matching) => matching.some((item) => item.effect === "deny"));
            const immutable = matchingByAction.some((matching) => matching.some((item) => item.immutable));
            const missingActions = selectedActionOptions.filter(
              ({ action }) => option.permissions === null || !option.permissions.has(action),
            );
            const eligible = missingActions.length === 0;
            const ineffective = allowedCount > 0 && !eligible;
            return <label className={styles.audienceCard} data-selected={allowed && eligible} data-partial={partiallyAllowed} data-denied={denied} data-ineligible={!eligible} data-ineffective={ineffective} key={`${option.type}-${option.id}`}>
              <input type="checkbox" checked={allowed} disabled={disabled || saving || immutable || (!eligible && !allowed)} onChange={() => toggleAllow(option)} />
              <span><strong>{option.label}</strong><small>{eligible ? option.detail : `本身缺少“${missingActions.map((item) => item.label).join("、")}”权限`}</small></span>
              <em>{immutable ? "系统授权" : ineffective ? "已有授权不会生效" : denied ? "包含禁止规则" : allowed ? "已全部允许" : partiallyAllowed ? `已授权 ${allowedCount}/${selectedActionOptions.length}` : eligible ? "可授权" : "不可授权"}</em>
            </label>;
          })}
        </div>
      ) : <div className={styles.emptyAudience}><strong>{selectedAudience === "account" ? "暂无可选用户" : "暂无权限组"}</strong><span>{selectedAudience === "account" ? "当前账号无权读取成员列表，可以改用权限组授权。" : "请先创建权限组，再回到这里分配访问范围。"}</span></div>}

      {deniedBindings.length ? <details className={styles.advanced}>
        <summary><span>高级限制</span><strong>{deniedBindings.length} 条明确禁止规则</strong></summary>
        <p>禁止规则优先于允许规则。普通运营通常不需要设置它。</p>
        <ul>{deniedBindings.map(({ action, binding }) => <li key={`${action.action}-${binding.principal_type}-${binding.principal_id}`}><span>{action.label}</span><strong>{principalLabel(binding)}</strong><em>禁止访问</em><Button type="button" hasIconOnly kind="ghost" size="sm" renderIcon={TrashCan} iconDescription={binding.immutable ? "系统限制不可移除" : "移除限制"} disabled={disabled || saving || binding.immutable} onClick={() => removeBinding(action.action, binding)} /></li>)}</ul>
      </details> : null}

      <div className={styles.actions}>
        <span>{dirtyActions.size ? `${dirtyActions.size} 项修改尚未保存` : "所有修改均已保存"}</span>
        <Button type="button" kind="ghost" size="sm" renderIcon={Renew} disabled={disabled || loading || saving} onClick={() => void load()}>重新载入</Button>
        <Button type="button" size="sm" disabled={disabled || loading || saving || !dirtyActions.size} onClick={() => void saveAll()}>{saving ? "正在保存…" : "保存访问范围"}</Button>
      </div>
    </section>
  );
}
