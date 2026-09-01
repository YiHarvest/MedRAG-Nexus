"use client";

import { Add, Edit, Renew, Settings, TrashCan, UserMultiple } from "@carbon/icons-react";
import { Button, InlineNotification, PasswordInput, Select, SelectItem, TextArea, TextInput } from "@carbon/react";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useAccount } from "@/components/account";
import { PolicyBindingsEditor, type PolicyActionOption } from "@/components/policy-bindings-editor";
import { EmptyState, LoadingState } from "@/components/states";
import { useWorkspace } from "@/components/workspace";
import {
  type AdminAccount, createPermissionGroup, createWebUiAccount,
  deletePermissionGroup, leaveOwnPermissionGroup, listWebUiAccounts, patchPermissionGroup, patchWebUiAccount,
  resetWebUiAccountPassword, setWebUiAccountBindings,
} from "@/lib/webui-admin-api";
import {
  registerKnowledgeDomain, registerKnowledgeBase, deleteWebUiKnowledgeUser, deleteWebUiWorkspace, describeWebUiError,
  getPermissionCatalog, leaveWebUiKnowledgeUser, leaveWebUiWorkspace, type PermissionCatalog, type PermissionGroupDefinition,
  type PermissionLevelDefinition, type PermissionNodeDefinition, type WebUiWorkspace,
  type WebUiWorkspaceUser, renameWebUiKnowledgeUser, renameWebUiWorkspace, updateWebUiUserPolicy,
  updateWebUiWorkspacePolicy,
} from "@/lib/webui-api";
import { generateUUID } from "@/lib/uuid";
import styles from "./management-view.module.css";

const EMPTY_CATALOG: PermissionCatalog = { levels: [], nodes: [], groups: [], plugins: [] };
const DEFAULT_LEVELS: PermissionLevelDefinition[] = [
  { level: 0, key: "basic", label: "初级用户", description: "默认注册账号。", plugin_id: "webui.core", available: true },
  { level: 1, key: "vip", label: "VIP 用户", description: "知识资源编辑者。", plugin_id: "webui.core", available: true },
  { level: 2, key: "workspace-manager", label: "知识库管理员", description: "可管理知识库的成员。", plugin_id: "webui.core", available: true },
  { level: 1000, key: "superadmin", label: "超级管理员", description: "系统管理等级；固定拥有全部系统权限。", plugin_id: "webui.core", available: true },
];
const LABELS: Record<string, [string, string]> = {
  "webui.account.create": ["创建用户", "创建新的登录用户。"],
  "webui.account.create_superadmin": ["创建超级管理员", "创建拥有全部系统权限的超级管理员。"],
  "webui.account.manage": ["管理用户", "查看和修改普通登录用户。"],
  "webui.account.update_self": ["修改个人资料", "修改当前用户自己的资料。"],
  "webui.account.password.change_self": ["修改个人密码", "修改当前用户自己的登录密码。"],
  "webui.account.password.reset": ["重置用户密码", "为普通用户重置登录密码。"],
  "webui.audit.read": ["查看审计记录", "查看 WebUI 中的安全审计记录。"],
  "webui.permission.catalog.read": ["查看权限配置", "查看系统提供的权限和权限组。"],
  "webui.permission.group.manage": ["管理权限组", "创建、修改和删除自定义权限组。"],
  "webui.chat.use": ["使用知识助手", "使用按照用户权限过滤知识范围的智能助手。"],
  "webui.retrieval.use": ["使用文档检索", "在用户获准访问的知识库中进行检索。"],
  "webui.system.read": ["查看系统状态", "查看 WebUI 服务及依赖的运行状态。"],
  "webui.user.read": ["查看知识域", "查看已授权知识域，并作为访问知识库的第一层校验。"],
  "webui.user.create": ["创建知识域", "创建新的知识域。"],
  "webui.user.rename": ["知识域改名", "修改知识域显示名称，编号和知识库保持不变。"],
  "webui.user.delete": ["删除知识域", "删除知识域及其全部知识库、内容和授权。"],
  "webui.user.binding.manage": ["绑定账号与知识域", "将普通登录账号绑定到一个或多个知识域。"],
  "webui.workspace.create": ["新建知识库", "在已授权知识域下新建知识库。"],
  "webui.user.policy.manage": ["管理知识域权限", "修改知识域的等级门槛与访问名单。"],
  "webui.workspace.read": ["查看知识库", "查看和检索已授权知识库的内容。"],
  "webui.workspace.rename": ["知识库改名", "修改展示名称，不改变 ID、目录和向量。"],
  "webui.workspace.delete": ["删除知识库", "删除知识库及其全部存储数据。"],
  "webui.workspace.policy.manage": ["管理知识库权限", "修改知识库的等级门槛与访问名单。"],
  "webui.resource.file.add": ["上传文件", "向知识库上传完整文件。"],
  "webui.resource.file.delete": ["删除文件", "删除知识库中的整个文件。"],
  "webui.resource.text.add": ["添加文本", "向知识库添加整段文本。"],
  "webui.resource.text.delete": ["删除文本", "删除知识库中的整段文本。"],
};
const USER_ACTIONS = [
  "webui.user.read",
  "webui.workspace.create",
  "webui.user.rename",
  "webui.user.delete",
  "webui.user.policy.manage",
];
const WORKSPACE_ACTIONS = [
  "webui.workspace.read",
  "webui.workspace.rename",
  "webui.workspace.delete",
  "webui.workspace.policy.manage",
  "webui.resource.file.add",
  "webui.resource.file.delete",
  "webui.resource.text.add",
  "webui.resource.text.delete",
];

interface Notice { kind: "success" | "error" | "warning"; title: string; detail: string }
interface AccountForm { loginName: string; displayName: string; password: string; level: string; groups: string[] }
interface GroupForm { key: string; name: string; description: string; permissions: string[] }
const EMPTY_ACCOUNT: AccountForm = { loginName: "", displayName: "", password: "", level: "0", groups: [] };
const EMPTY_GROUP: GroupForm = { key: "", name: "", description: "", permissions: [] };

const PERMISSION_SECTIONS = [
  {
    key: "basic",
    title: "基本权限",
    description: "登录后的通用功能与个人设置。",
    permissions: [
      "webui.chat.use", "webui.retrieval.use", "webui.system.read",
      "webui.account.update_self", "webui.account.password.change_self",
      "webui.permission.catalog.read",
    ],
  },
  {
    key: "files",
    title: "文件权限",
    description: "维护知识库中的文件和文本内容。",
    permissions: [
      "webui.resource.file.add", "webui.resource.file.delete",
      "webui.resource.text.add", "webui.resource.text.delete",
    ],
  },
  {
    key: "workspaces",
    title: "知识库权限",
    description: "查看、创建、改名、删除知识库及配置其权限。",
    permissions: [
      "webui.workspace.read", "webui.workspace.create", "webui.workspace.rename",
      "webui.workspace.delete", "webui.workspace.policy.manage",
    ],
  },
  {
    key: "domains",
    title: "知识域权限",
    description: "查看、创建、改名、删除知识域及配置其权限。",
    permissions: [
      "webui.user.read", "webui.user.create", "webui.user.rename",
      "webui.user.delete", "webui.user.policy.manage",
    ],
  },
  {
    key: "members",
    title: "成员管理权限",
    description: "管理登录用户、权限组、密码与普通账号的知识域绑定。",
    permissions: [
      "webui.account.create", "webui.account.create_superadmin", "webui.account.manage",
      "webui.account.password.reset", "webui.audit.read",
      "webui.permission.group.manage", "webui.user.binding.manage",
    ],
  },
] as const;

function permissionPresentation(key: string, nodes: PermissionNodeDefinition[]) {
  const node = nodes.find((item) => item.key === key);
  const configured = LABELS[key];
  const readableNodeLabel = node?.label && !node.label.startsWith("webui.") ? node.label : undefined;
  return {
    label: configured?.[0] ?? readableNodeLabel ?? node?.description ?? "扩展权限",
    description: configured?.[1] ?? node?.description ?? "由权限插件提供的扩展能力。",
    node,
  };
}

function groupLabel(group: PermissionGroupDefinition): string {
  if (group.name && !group.name.startsWith("webui.")) return group.name;
  return group.description || "自定义权限组";
}

function groupLabelByKey(key: string, groups: PermissionGroupDefinition[]): string {
  const group = groups.find((item) => item.key === key);
  return group ? groupLabel(group) : "自定义权限组";
}

function levelLabel(level: number, levels: PermissionLevelDefinition[]): string {
  const matched = levels.find((item) => item.level === level);
  return matched ? `${matched.label}（${level} 级）` : `${level} 级`;
}

function actionOption(key: string, nodes: PermissionNodeDefinition[]): PolicyActionOption {
  const { label, description, node } = permissionPresentation(key, nodes);
  return { action: key, label, description, available: node?.available ?? false };
}
function toggle(values: string[], value: string) { return values.includes(value) ? values.filter((item) => item !== value) : [...values, value]; }
function protectedAccount(item: AdminAccount) { return item.capabilities?.protected ?? item.permission_level === 1000; }

export function ManagementView() {
  const { account, can, refresh: refreshPrincipal } = useAccount();
  const { workspaceOptions, workspaceUsers, refreshWorkspaces } = useWorkspace();
  const actorLevel = account?.permission_level ?? 0;
  const canManageAccounts = can("webui.account.manage");
  const canCreateAccounts = can("webui.account.create");
  const canResetPasswords = can("webui.account.password.reset");
  const isSuperadmin = actorLevel === 1000;
  const canManageGroups = isSuperadmin && can("webui.permission.group.manage");
  const canCreateUser = can("webui.user.create");
  const canManageUserPolicies = can("webui.user.policy.manage");
  const [catalog, setCatalog] = useState<PermissionCatalog>(EMPTY_CATALOG);
  const [accounts, setAccounts] = useState<AdminAccount[]>([]);
  const [selectedLevel, setSelectedLevel] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const [accountOpen, setAccountOpen] = useState(false);
  const [accountForm, setAccountForm] = useState<AccountForm>(EMPTY_ACCOUNT);
  const [editAccount, setEditAccount] = useState<AdminAccount | null>(null);
  const [editName, setEditName] = useState("");
  const [editLevel, setEditLevel] = useState("0");
  const [editGroups, setEditGroups] = useState<string[]>([]);
  const [editEnabled, setEditEnabled] = useState(true);
  const [resetAccount, setResetAccount] = useState<AdminAccount | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [bindingAccount, setBindingAccount] = useState<AdminAccount | null>(null);
  const [bindingUserIds, setBindingUserIds] = useState<string[]>([]);
  const [groupOpen, setGroupOpen] = useState(false);
  const [editGroup, setEditGroup] = useState<PermissionGroupDefinition | null>(null);
  const [groupForm, setGroupForm] = useState<GroupForm>(EMPTY_GROUP);
  const [userOpen, setUserOpen] = useState(false);
  const [userName, setUserName] = useState("");
  const [userAccount, setUserAccount] = useState("");
  const [newWorkspaceUser, setNewWorkspaceUser] = useState<WebUiWorkspaceUser | null>(null);
  const [workspaceName, setWorkspaceName] = useState("");
  const [workspaceRead, setWorkspaceRead] = useState("0");
  const [workspaceWrite, setWorkspaceWrite] = useState("0");
  const [userPolicy, setUserPolicy] = useState<WebUiWorkspaceUser | null>(null);
  const [userRead, setUserRead] = useState("0");
  const [userCreate, setUserCreate] = useState("0");
  const [renameUser, setRenameUser] = useState<WebUiWorkspaceUser | null>(null);
  const [renameUserName, setRenameUserName] = useState("");
  const [renameWorkspace, setRenameWorkspace] = useState<WebUiWorkspace | null>(null);
  const [renameWorkspaceName, setRenameWorkspaceName] = useState("");
  const [workspacePolicy, setWorkspacePolicy] = useState<WebUiWorkspace | null>(null);
  const [policyRead, setPolicyRead] = useState("0");
  const [policyWrite, setPolicyWrite] = useState("0");

  const refreshCatalog = useCallback(async () => setCatalog(await getPermissionCatalog()), []);
  const refreshAccounts = useCallback(async () => {
    if (!canManageAccounts) return;
    setAccounts((await listWebUiAccounts()).accounts);
  }, [canManageAccounts]);
  const reload = useCallback(async () => {
    setLoading(true);
    try { await Promise.all([refreshCatalog(), refreshAccounts(), refreshWorkspaces()]); }
    catch (error) { setNotice({ kind: "error", title: "管理数据读取失败", detail: describeWebUiError(error) }); }
    finally { setLoading(false); }
  }, [refreshAccounts, refreshCatalog, refreshWorkspaces]);
  useEffect(() => { const timer = window.setTimeout(() => void reload(), 0); return () => window.clearTimeout(timer); }, [reload]);

  const visibleAccounts = canManageAccounts ? accounts : account ? [{
    account_id: account.account_id, login_name: account.login_name, display_name: account.display_name,
    permission_level: account.permission_level, enabled: true, bound_user_id: account.bound_user_id,
    must_change_password: Boolean(account.must_change_password), password_changed_at: "", last_login_at: null,
    created_at: "", modified_at: "", groups: account.groups,
    bound_user_ids: account.bound_user_ids ?? [],
  }] satisfies AdminAccount[] : [];
  const levels = (catalog.levels.length ? catalog.levels : DEFAULT_LEVELS).filter((item) => item.available);
  const allowedLevels = levels.filter((item) => item.level <= actorLevel);
  const groups = catalog.groups;
  const selectedLevelDefinition = selectedLevel === null
    ? undefined
    : levels.find((level) => level.level === selectedLevel);
  const customGroups = groups;
  const userActions = USER_ACTIONS.map((key) => actionOption(key, catalog.nodes));
  const workspaceActions = WORKSPACE_ACTIONS.map((key) => actionOption(key, catalog.nodes));
  const byUser = useMemo(() => {
    const result = new Map<string, WebUiWorkspace[]>();
    for (const item of workspaceOptions) result.set(item.user_id, [...(result.get(item.user_id) ?? []), item]);
    return result;
  }, [workspaceOptions]);

  async function perform(operation: () => Promise<void>, failureTitle: string) {
    setBusy(true);
    try { await operation(); } catch (error) { setNotice({ kind: "error", title: failureTitle, detail: describeWebUiError(error) }); }
    finally { setBusy(false); }
  }

  async function submitAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await perform(async () => {
      await createWebUiAccount({ login_name: accountForm.loginName.trim(), display_name: accountForm.displayName.trim(), password: accountForm.password, permission_level: Number(accountForm.level), group_keys: accountForm.groups, must_change_password: true });
      setAccountForm(EMPTY_ACCOUNT); setAccountOpen(false);
      setNotice({ kind: "success", title: "用户已创建", detail: "现在可以继续设置等级和权限组。" });
      await refreshAccounts();
    }, "用户创建失败");
  }
  async function submitAccountEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!editAccount || protectedAccount(editAccount)) return;
    await perform(async () => {
      await patchWebUiAccount(editAccount.account_id, {
        display_name: editName.trim(),
        permission_level: Number(editLevel),
        enabled: editEnabled,
        group_keys: editLevel === "1000" ? [] : editGroups,
      });
      if (editAccount.account_id === account?.account_id) await refreshPrincipal();
      setEditAccount(null); setNotice({ kind: "success", title: "用户已更新", detail: "等级与多个权限组已保存。" }); await refreshAccounts();
    }, "用户更新失败");
  }
  async function submitReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!resetAccount || protectedAccount(resetAccount)) return;
    await perform(async () => { await resetWebUiAccountPassword(resetAccount.account_id, { new_password: resetPassword, must_change_password: true }); setResetAccount(null); setResetPassword(""); setNotice({ kind: "success", title: "密码已重置", detail: "旧会话已撤销。" }); await refreshAccounts(); }, "密码重置失败");
  }
  async function submitBinding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!bindingAccount || protectedAccount(bindingAccount)) return;
    await perform(async () => {
      await setWebUiAccountBindings(bindingAccount.account_id, bindingUserIds);
      setBindingAccount(null);
      setBindingUserIds([]);
      setNotice({
        kind: "success",
        title: bindingUserIds.length ? "知识域绑定已更新" : "知识域绑定已清空",
        detail: bindingAccount.display_name,
      });
      await Promise.all([refreshAccounts(), refreshWorkspaces()]);
    }, "知识域绑定失败");
  }
  function openGroup(group?: PermissionGroupDefinition) {
    setEditGroup(group ?? null); setGroupForm(group ? { key: group.key, name: groupLabel(group), description: group.description, permissions: group.permissions } : EMPTY_GROUP); setGroupOpen(true);
  }
  async function submitGroup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (editGroup?.immutable) return;
    await perform(async () => { const payload = { name: groupForm.name.trim(), description: groupForm.description.trim(), permissions: groupForm.permissions }; if (editGroup) await patchPermissionGroup(editGroup.key, payload); else await createPermissionGroup({ key: `webui.custom.${generateUUID().replaceAll("-", "")}`, ...payload }); setGroupOpen(false); setEditGroup(null); setGroupForm(EMPTY_GROUP); setNotice({ kind: "success", title: "权限组已保存", detail: "多组权限节点按并集合并。" }); await refreshCatalog(); }, "权限组保存失败");
  }
  async function removeGroup(group: PermissionGroupDefinition) {
    if (group.immutable || !window.confirm(`确认删除权限组“${groupLabel(group)}”？`)) return;
    await perform(async () => { await deletePermissionGroup(group.key); setNotice({ kind: "success", title: "权限组已删除", detail: groupLabel(group) }); await refreshCatalog(); }, "权限组删除失败");
  }
  async function removeGroupMember(group: PermissionGroupDefinition, member: AdminAccount) {
    if (!isSuperadmin || protectedAccount(member) || !window.confirm(`确认将“${member.display_name}”移出“${groupLabel(group)}”？`)) return;
    await perform(async () => {
      await patchWebUiAccount(member.account_id, {
        display_name: member.display_name,
        permission_level: member.permission_level,
        enabled: member.enabled,
        group_keys: member.groups.filter((key) => key !== group.key),
      });
      setNotice({ kind: "success", title: "成员已移出权限组", detail: `${member.display_name} · ${groupLabel(group)}` });
      await refreshAccounts();
    }, "移出成员失败");
  }
  async function leaveGroup(group: PermissionGroupDefinition) {
    if (!account?.groups.includes(group.key) || !window.confirm(`确认退出“${groupLabel(group)}”？退出后会立即失去该组提供的全部权限。`)) return;
    await perform(async () => {
      await leaveOwnPermissionGroup(group.key);
      await refreshPrincipal();
      await refreshWorkspaces();
      setNotice({ kind: "success", title: "已退出权限组", detail: groupLabel(group) });
    }, "退出权限组失败");
  }
  async function submitUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await perform(async () => { await registerKnowledgeDomain({ user_name: userName.trim(), bind_account_id: userAccount || null }); setUserOpen(false); setUserName(""); setUserAccount(""); setNotice({ kind: "success", title: "知识域已创建", detail: "可以继续在该知识域中新建知识库。" }); await reload(); }, "知识域创建失败");
  }
  async function submitWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!newWorkspaceUser) return;
    await perform(async () => { await registerKnowledgeBase({ user_id: newWorkspaceUser.user_id, workspace_name: workspaceName.trim(), read_min_level: Number(workspaceRead), cud_min_level: Number(workspaceWrite) }); setNewWorkspaceUser(null); setNotice({ kind: "success", title: "知识库已创建", detail: workspaceName }); setWorkspaceName(""); await refreshWorkspaces(); }, "知识库创建失败");
  }
  async function submitUserPolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!userPolicy) return;
    await perform(async () => { await updateWebUiUserPolicy(userPolicy.user_id, { read_min_level: Number(userRead), workspace_create_min_level: Number(userCreate) }); setUserPolicy(null); setNotice({ kind: "success", title: "知识域权限已更新", detail: userPolicy.user_name }); await refreshWorkspaces(); }, "知识域权限更新失败");
  }
  async function submitUserRename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!renameUser) return;
    const nextName = renameUserName.trim();
    if (!nextName) return;
    await perform(async () => {
      await renameWebUiKnowledgeUser(renameUser.user_id, nextName);
      setRenameUser(null); setRenameUserName("");
      setNotice({ kind: "success", title: "知识域已改名", detail: nextName });
      await refreshWorkspaces();
    }, "知识域改名失败");
  }
  async function submitWorkspacePolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!workspacePolicy) return;
    await perform(async () => { await updateWebUiWorkspacePolicy(workspacePolicy.workspace_id, { read_min_level: Number(policyRead), cud_min_level: Number(policyWrite) }); setWorkspacePolicy(null); setNotice({ kind: "success", title: "知识库权限已更新", detail: workspacePolicy.workspace_name }); await refreshWorkspaces(); }, "知识库权限更新失败");
  }
  async function submitWorkspaceRename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!renameWorkspace) return;
    const nextName = renameWorkspaceName.trim();
    if (!nextName) return;
    await perform(async () => {
      await renameWebUiWorkspace(renameWorkspace.workspace_id, nextName);
      setRenameWorkspace(null); setRenameWorkspaceName("");
      setNotice({ kind: "success", title: "知识库已改名", detail: nextName });
      await refreshWorkspaces();
    }, "知识库改名失败");
  }
  async function removeWorkspace(item: WebUiWorkspace) {
    if (!window.confirm(`确认删除知识库“${item.workspace_name}”？`)) return;
    await perform(async () => { const result = await deleteWebUiWorkspace(item.workspace_id, item.workspace_name); setNotice(result.status === "deleted" ? { kind: "success", title: "知识库已删除", detail: item.workspace_name } : { kind: "warning", title: "知识库已隐藏", detail: "后台正在清理所有存储。" }); await refreshWorkspaces(); }, "知识库删除失败");
  }
  async function removeKnowledgeUser(item: WebUiWorkspaceUser) {
    const count = byUser.get(item.user_id)?.length ?? 0;
    if (!window.confirm(`确认删除知识域“${item.user_name}”？这会同时永久删除其中 ${count} 个知识库及全部内容。`)) return;
    await perform(async () => {
      const result = await deleteWebUiKnowledgeUser(item.user_id, item.user_name);
      setNotice(result.status === "deleted"
        ? { kind: "success", title: "知识域已删除", detail: `${item.user_name}（同时删除 ${result.deleted_workspace_count} 个知识库）` }
        : { kind: "warning", title: "知识域清理尚未完成", detail: "部分底层存储正在后台恢复清理，请稍后重试。" });
      await reload();
    }, "知识域删除失败");
  }
  async function leaveKnowledgeUser(item: WebUiWorkspaceUser) {
    if (isSuperadmin || !window.confirm(`确认退出知识域“${item.user_name}”？退出后将无法访问其下全部知识库，知识助手也不会再检索这些内容。`)) return;
    await perform(async () => {
      await leaveWebUiKnowledgeUser(item.user_id);
      await refreshPrincipal();
      await refreshWorkspaces();
      setNotice({ kind: "success", title: "已退出知识域", detail: item.user_name });
    }, "退出知识域失败");
  }
  async function leaveWorkspace(item: WebUiWorkspace) {
    if (isSuperadmin || !window.confirm(`确认退出知识库“${item.workspace_name}”？退出后将无法查看、维护或通过知识助手检索此知识库。`)) return;
    await perform(async () => {
      await leaveWebUiWorkspace(item.workspace_id);
      await refreshWorkspaces();
      setNotice({ kind: "success", title: "已退出知识库", detail: item.workspace_name });
    }, "退出知识库失败");
  }

  return <div className={`page ${styles.page}`}>
    {notice ? <InlineNotification kind={notice.kind} title={notice.title} subtitle={notice.detail} onCloseButtonClick={() => setNotice(null)} /> : null}
    <section className={styles.levelSection}>
      <Heading title="成员等级" detail="点击等级可查看该等级对应的系统固定权限；实际访问的知识内容仍需资源授权。"><Button kind="ghost" renderIcon={Renew} disabled={loading || busy} onClick={() => void reload()}>刷新</Button></Heading>
      <div className={styles.levelGrid}>{levels.map((item) => {
        const expanded = item.level === selectedLevel;
        return <button type="button" className={styles.levelCard} aria-expanded={expanded} data-current={item.level === actorLevel} data-selected={expanded} key={item.key} onClick={() => setSelectedLevel(expanded ? null : item.level)}><header><strong>{item.level}</strong>{item.level === actorLevel ? <span>当前等级</span> : <span>{expanded ? "收起权限" : "查看权限"}</span>}</header><h3>{item.label}</h3><p>{item.description}</p></button>;
      })}</div>
      {selectedLevel !== null ? <article className={styles.levelPermissionPanel}>
        <header><div><span>等级 {selectedLevel}</span><h3>{selectedLevelDefinition?.label ?? "成员等级"}</h3><p>{selectedLevelDefinition?.description ?? "该成员等级当前不可用。"}</p></div><em>等级固有权限</em></header>
        {selectedLevelDefinition ? <PermissionGroupSummary permissions={selectedLevelDefinition.permissions ?? []} nodes={catalog.nodes} /> : null}
      </article> : null}
      <div className={styles.summaryBar}><div><span>登录用户</span><strong>{visibleAccounts.length}</strong></div><div><span>权限组</span><strong>{groups.length}</strong></div><div><span>知识域</span><strong>{workspaceUsers.length}</strong></div><div><span>知识库</span><strong>{workspaceOptions.length}</strong></div></div>
    </section>

    <section>
      <Heading title="自定义权限组" detail="按业务需要组合额外权限；成员加入多个组时，权限会自动合并。">{canManageGroups ? <Button renderIcon={Add} onClick={() => openGroup()}>新建权限组</Button> : null}</Heading>
      {groupOpen ? <form className={styles.formPanel} onSubmit={submitGroup}><h3>{editGroup ? `编辑权限组 · ${groupLabel(editGroup)}` : "新建自定义权限组"}</h3>{editGroup?.immutable ? <InlineNotification lowContrast hideCloseButton kind="warning" title="系统权限组不可修改" subtitle="系统固定权限不能改变。" /> : null}<div className={styles.formGrid}><TextInput id="group-name" labelText="权限组名称" helperText="必填，将显示在用户分配和访问名单中。" required value={groupForm.name} disabled={busy || editGroup?.immutable} onChange={(e) => setGroupForm((v) => ({ ...v, name: e.target.value }))} /><TextArea id="group-description" labelText="权限组说明（可选）" rows={2} value={groupForm.description} disabled={busy || editGroup?.immutable} onChange={(e) => setGroupForm((v) => ({ ...v, description: e.target.value }))} /></div><p className={styles.selectionHint}>按业务范围选择权限；保存后可在“新建用户”或“修改用户”中分配，多组权限按并集合并。</p><PermissionNodeSections nodes={catalog.nodes} selected={groupForm.permissions} disabled={busy || Boolean(editGroup?.immutable)} onToggle={(key) => setGroupForm((value) => ({ ...value, permissions: toggle(value.permissions, key) }))} /><div className={styles.formActions}><Button type="submit" disabled={busy || editGroup?.immutable || !groupForm.name.trim() || !groupForm.permissions.length}>保存权限组</Button><Button type="button" kind="ghost" onClick={() => setGroupOpen(false)}>取消</Button></div></form> : null}
      {customGroups.length ? <div className={styles.groupGrid}>{customGroups.map((group) => <PermissionGroupCard key={group.key} group={group} members={accounts.filter((item) => item.groups.includes(group.key))} nodes={catalog.nodes} canManage={canManageGroups} isMember={Boolean(account?.groups.includes(group.key))} busy={busy} onEdit={() => openGroup(group)} onDelete={() => void removeGroup(group)} onRemoveMember={(member) => void removeGroupMember(group, member)} onLeave={() => void leaveGroup(group)} />)}</div> : <div className={styles.emptyCustomGroups}><strong>暂无自定义权限组</strong><span>需要组合额外权限时，可以从右上角新建。</span></div>}
    </section>

    <section>
      <Heading title="用户管理" detail="管理登录用户、成员等级、组织权限组和密码。">{canCreateAccounts ? <Button renderIcon={Add} onClick={() => setAccountOpen(!accountOpen)}>{accountOpen ? "收起" : "新建用户"}</Button> : null}</Heading>
      {accountOpen ? <form className={styles.formPanel} onSubmit={submitAccount}><h3>新建登录用户</h3><div className={styles.formGrid}><TextInput id="account-login" labelText="用户名" required value={accountForm.loginName} disabled={busy} onChange={(e) => setAccountForm((v) => ({ ...v, loginName: e.target.value }))} /><TextInput id="account-display" labelText="显示名称" required value={accountForm.displayName} disabled={busy} onChange={(e) => setAccountForm((v) => ({ ...v, displayName: e.target.value }))} /><PasswordInput id="account-password" labelText="初始密码" minLength={3} required value={accountForm.password} disabled={busy} showPasswordLabel="显示" hidePasswordLabel="隐藏" onChange={(e) => setAccountForm((v) => ({ ...v, password: e.target.value }))} /><LevelSelect id="account-level" label="权限等级" levels={allowedLevels} value={accountForm.level} disabled={busy} onChange={(level) => setAccountForm((v) => ({ ...v, level, groups: level === "1000" ? [] : v.groups }))} /></div><GroupChoices groups={groups} selected={accountForm.groups} disabled={busy || accountForm.level === "1000"} onChange={(next) => setAccountForm((v) => ({ ...v, groups: next }))} /><div className={styles.formActions}><Button type="submit" disabled={busy || accountForm.password.length < 3}>创建用户</Button><Button type="button" kind="ghost" onClick={() => setAccountOpen(false)}>取消</Button></div></form> : null}
      {editAccount ? <form className={styles.formPanel} onSubmit={submitAccountEdit}><h3>修改用户 · {editAccount.login_name}</h3>{protectedAccount(editAccount) ? <InlineNotification lowContrast hideCloseButton kind="warning" title="超级管理员不可修改" subtitle="等级、权限组、状态和绑定全部固定。" /> : null}<div className={styles.formGrid}><TextInput id="edit-name" labelText="显示名称" required value={editName} disabled={busy || protectedAccount(editAccount)} onChange={(e) => setEditName(e.target.value)} /><LevelSelect id="edit-level" label="权限等级" levels={allowedLevels} value={editLevel} disabled={busy || protectedAccount(editAccount)} onChange={(level) => { setEditLevel(level); if (level === "1000") setEditGroups([]); }} /><label className={styles.checkbox}><input type="checkbox" checked={editEnabled} disabled={busy || protectedAccount(editAccount)} onChange={(e) => setEditEnabled(e.target.checked)} /><span><strong>启用用户</strong><small>停用后不能登录</small></span></label></div><GroupChoices groups={groups} selected={editGroups} disabled={busy || protectedAccount(editAccount) || editLevel === "1000"} onChange={setEditGroups} /><div className={styles.formActions}><Button type="submit" disabled={busy || protectedAccount(editAccount)}>保存</Button><Button type="button" kind="ghost" onClick={() => setEditAccount(null)}>取消</Button></div></form> : null}
      {resetAccount ? <form className={styles.formPanel} onSubmit={submitReset}><h3>重置密码 · {resetAccount.login_name}</h3><PasswordInput id="reset-password" labelText="新密码" minLength={3} required value={resetPassword} disabled={busy} showPasswordLabel="显示" hidePasswordLabel="隐藏" onChange={(e) => setResetPassword(e.target.value)} /><div className={styles.formActions}><Button type="submit" kind="danger" disabled={busy || resetPassword.length < 3}>重置密码</Button><Button type="button" kind="ghost" onClick={() => setResetAccount(null)}>取消</Button></div></form> : null}
      {bindingAccount ? <form className={styles.formPanel} onSubmit={submitBinding}><h3>绑定知识域 · {bindingAccount.login_name}</h3><KnowledgeDomainChoices users={workspaceUsers} selected={bindingUserIds} disabled={busy} onChange={setBindingUserIds} /><p className={styles.selectionHint}>普通账号可绑定 0～N 个知识域；同一知识域也可以绑定多个普通账号。</p><div className={styles.formActions}><Button type="submit" disabled={busy}>保存绑定</Button><Button type="button" kind="ghost" onClick={() => setBindingAccount(null)}>取消</Button></div></form> : null}
      {loading ? <LoadingState label="正在读取用户" /> : visibleAccounts.length ? <AccountCards accounts={visibleAccounts} groups={groups} users={workspaceUsers} levels={levels} canManage={canManageAccounts} canReset={canResetPasswords} onEdit={(item) => { setEditAccount(item); setEditName(item.display_name); setEditLevel(String(item.permission_level)); setEditGroups(item.groups); setEditEnabled(item.enabled); }} onReset={(item) => { setResetAccount(item); setResetPassword(""); }} onBinding={(item) => { setBindingAccount(item); setBindingUserIds(item.bound_user_ids); }} /> : <EmptyState title="暂无可见用户" description="当前用户没有可展示的登录用户。" icon={<UserMultiple size={32} />} />}
    </section>

    <section>
      <Heading title="知识域与知识库管理" detail="知识域用于归类一组相关知识库；超级管理员可以管理全部知识域。">{canCreateUser ? <Button renderIcon={Add} onClick={() => setUserOpen(!userOpen)}>{userOpen ? "收起" : "新建知识域"}</Button> : null}</Heading>
      {userOpen ? <form className={styles.formPanel} onSubmit={submitUser}><h3>新建知识域</h3><div className={styles.formGrid}><TextInput id="user-name" labelText="知识域名称" required value={userName} disabled={busy} onChange={(e) => setUserName(e.target.value)} />{can("webui.user.binding.manage") ? <Select id="user-account" labelText="绑定普通账号（可选）" value={userAccount} disabled={busy} onChange={(e) => setUserAccount(e.target.value)}><SelectItem value="" text="不绑定账号" />{visibleAccounts.filter((item) => item.permission_level < 1000).map((item) => <SelectItem key={item.account_id} value={item.account_id} text={`${item.display_name}（${item.login_name}）`} />)}</Select> : null}</div><div className={styles.formActions}><Button type="submit" disabled={busy || !userName.trim()}>创建知识域</Button><Button type="button" kind="ghost" onClick={() => setUserOpen(false)}>取消</Button></div></form> : null}
      {userPolicy ? <form className={styles.formPanel} onSubmit={submitUserPolicy}>
        <div className={styles.policyFormHeading}><span>知识域访问</span><h3>设置“{userPolicy.user_name}”的访问范围</h3><p>选择一项或多项权限，再勾选可以执行这些操作的用户或权限组。</p></div>
        <PolicyBindingsEditor resourceType="user" resourceId={userPolicy.user_id} actions={userActions} accounts={accounts} groups={groups} canListAccounts={canManageAccounts} disabled={busy} onSaved={() => void refreshWorkspaces()} />
        <details className={styles.advancedPolicySettings}>
          <summary><span>高级设置：成员等级门槛</span><small>通常无需修改</small></summary>
          <div className={styles.advancedPolicyBody}><p>只有达到等级门槛且出现在上方允许名单中的成员才能访问。</p><div className={styles.formGrid}><LevelSelect id="user-read" label="查看知识域的最低等级" levels={allowedLevels} value={userRead} disabled={busy} onChange={setUserRead} /><LevelSelect id="user-create" label="新建知识库的最低等级" levels={allowedLevels} value={userCreate} disabled={busy} onChange={setUserCreate} /></div><div className={styles.formActions}><Button type="submit" disabled={busy || !canManageUserPolicies}>保存等级门槛</Button></div></div>
        </details>
        <div className={styles.formActions}><Button type="button" kind="ghost" onClick={() => setUserPolicy(null)}>完成并关闭</Button></div>
      </form> : null}
      {renameUser ? <form className={styles.formPanel} onSubmit={submitUserRename}><h3>知识域改名 · {renameUser.user_name}</h3><TextInput id="rename-user-name" labelText="新名称" required value={renameUserName} disabled={busy} onChange={(event) => setRenameUserName(event.target.value)} /><p className={styles.selectionHint}>只修改显示名称，知识域编号、知识库、文件和向量数据都不会改变。</p><div className={styles.formActions}><Button type="submit" disabled={busy || !renameUserName.trim()}>保存名称</Button><Button type="button" kind="ghost" onClick={() => setRenameUser(null)}>取消</Button></div></form> : null}
      {renameWorkspace ? <form className={styles.formPanel} onSubmit={submitWorkspaceRename}><h3>知识库改名 · {renameWorkspace.workspace_name}</h3><TextInput id="rename-workspace-name" labelText="新名称" required value={renameWorkspaceName} disabled={busy} onChange={(event) => setRenameWorkspaceName(event.target.value)} /><p className={styles.selectionHint}>只修改显示名称，知识库编号、文件目录、索引和向量数据都不会改变。</p><div className={styles.formActions}><Button type="submit" disabled={busy || !renameWorkspaceName.trim()}>保存名称</Button><Button type="button" kind="ghost" onClick={() => setRenameWorkspace(null)}>取消</Button></div></form> : null}
      {workspacePolicy ? <form className={styles.formPanel} onSubmit={submitWorkspacePolicy}>
        <div className={styles.policyFormHeading}><span>知识库访问</span><h3>设置“{workspacePolicy.workspace_name}”的访问范围</h3><p>可同时选择查看、改名、删除、上传等多项权限，再统一分配用户或权限组。</p></div>
        <PolicyBindingsEditor resourceType="workspace" resourceId={workspacePolicy.workspace_id} actions={workspaceActions} accounts={accounts} groups={groups} canListAccounts={canManageAccounts} disabled={busy} onSaved={() => void refreshWorkspaces()} />
        <details className={styles.advancedPolicySettings}>
          <summary><span>高级设置：成员等级门槛</span><small>通常无需修改</small></summary>
          <div className={styles.advancedPolicyBody}><p>只有达到等级门槛且出现在上方允许名单中的成员才能访问。</p><div className={styles.formGrid}><LevelSelect id="policy-read" label="查看知识库的最低等级" levels={allowedLevels} value={policyRead} disabled={busy} onChange={setPolicyRead} /><LevelSelect id="policy-write" label="维护知识库的最低等级" levels={allowedLevels} value={policyWrite} disabled={busy} onChange={setPolicyWrite} /></div><div className={styles.formActions}><Button type="submit" disabled={busy}>保存等级门槛</Button></div></div>
        </details>
        <div className={styles.formActions}><Button type="button" kind="ghost" onClick={() => setWorkspacePolicy(null)}>完成并关闭</Button></div>
      </form> : null}
      {newWorkspaceUser ? <form className={styles.formPanel} onSubmit={submitWorkspace}><h3>在“{newWorkspaceUser.user_name}”中新建知识库</h3><div className={styles.formGrid}><TextInput id="workspace-name" labelText="知识库名称" required value={workspaceName} disabled={busy} onChange={(e) => setWorkspaceName(e.target.value)} /><LevelSelect id="workspace-read" label="允许查看的最低等级" levels={allowedLevels} value={workspaceRead} disabled={busy} onChange={setWorkspaceRead} /><LevelSelect id="workspace-write" label="允许维护的最低等级" levels={allowedLevels} value={workspaceWrite} disabled={busy} onChange={setWorkspaceWrite} /></div><div className={styles.formActions}><Button type="submit" disabled={busy || !workspaceName.trim()}>创建知识库</Button><Button type="button" kind="ghost" onClick={() => setNewWorkspaceUser(null)}>取消</Button></div></form> : null}
      <div className={styles.userGrid}>{workspaceUsers.map((user) => (
        <KnowledgeDomainCard
          key={user.user_id}
          user={user}
          boundAccounts={visibleAccounts.filter((item) => item.permission_level < 1000 && item.bound_user_ids.includes(user.user_id))}
          workspaces={byUser.get(user.user_id) ?? []}
          levels={levels}
          busy={busy}
          onManageDomain={() => { setUserPolicy(user); setUserRead(String(user.read_min_level)); setUserCreate(String(user.workspace_create_min_level)); }}
          onRenameDomain={() => { setRenameUser(user); setRenameUserName(user.user_name); }}
          onDeleteDomain={() => void removeKnowledgeUser(user)}
          canLeave={!isSuperadmin}
          onLeaveDomain={() => void leaveKnowledgeUser(user)}
          onCreateWorkspace={() => { setNewWorkspaceUser(user); setWorkspaceName(""); setWorkspaceRead("0"); setWorkspaceWrite("0"); }}
          onManageWorkspace={(item) => { setWorkspacePolicy(item); setPolicyRead(String(item.read_min_level)); setPolicyWrite(String(item.cud_min_level)); }}
          onRenameWorkspace={(item) => { setRenameWorkspace(item); setRenameWorkspaceName(item.workspace_name); }}
          onDeleteWorkspace={(item) => void removeWorkspace(item)}
          onLeaveWorkspace={(item) => void leaveWorkspace(item)}
        />
      ))}</div>
      {!workspaceUsers.length ? <EmptyState title="暂无可见知识域" description="当前用户还没有获得任何知识域的访问权限。" icon={<Settings size={32} />} /> : null}
    </section>
  </div>;
}

function Heading({ title, detail, children }: { title: string; detail: string; children?: React.ReactNode }) { return <div className={styles.sectionHeading}><div><h2>{title}</h2><p className={styles.sectionDescription}>{detail}</p></div>{children}</div>; }
function GroupChoices({ groups, selected, disabled, onChange }: { groups: PermissionGroupDefinition[]; selected: string[]; disabled: boolean; onChange: (value: string[]) => void }) { return <fieldset className={styles.choiceFieldset} disabled={disabled}><legend>组织权限组（可多选，也可以不加入）</legend>{groups.length ? <div className={styles.choiceGrid}>{groups.map((group) => <label className={styles.groupChoice} key={group.key}><input type="checkbox" checked={selected.includes(group.key)} onChange={() => onChange(toggle(selected, group.key))} /><span><strong>{groupLabel(group)}</strong><small>{group.description}</small></span></label>)}</div> : <p className={styles.selectionHint}>当前还没有组织权限组，可先创建成员，之后再加入权限组。</p>}</fieldset>; }
function KnowledgeDomainChoices({ users, selected, disabled, onChange }: { users: WebUiWorkspaceUser[]; selected: string[]; disabled: boolean; onChange: (value: string[]) => void }) { return <fieldset className={styles.choiceFieldset} disabled={disabled}><legend>选择知识域（可多选，也可以全部不选）</legend>{users.length ? <div className={styles.choiceGrid}>{users.map((user) => <label className={styles.groupChoice} key={user.user_id}><input type="checkbox" checked={selected.includes(user.user_id)} onChange={() => onChange(toggle(selected, user.user_id))} /><span><strong>{user.user_name}</strong><small>{user.user_id}</small></span></label>)}</div> : <p className={styles.selectionHint}>当前没有可绑定的知识域。</p>}</fieldset>; }
function PermissionGroupCard({ group, members, nodes, canManage, isMember, busy, onEdit, onDelete, onRemoveMember, onLeave }: { group: PermissionGroupDefinition; members: AdminAccount[]; nodes: PermissionNodeDefinition[]; canManage: boolean; isMember: boolean; busy: boolean; onEdit: () => void; onDelete: () => void; onRemoveMember: (member: AdminAccount) => void; onLeave: () => void }) {
  return <article className={styles.groupCard}>
    <header><div><strong>{groupLabel(group)}</strong></div><span>{group.permissions.length} 项权限</span></header>
    <p>{group.description || "暂无说明"}</p>
    {canManage ? <details className={`${styles.groupDisclosure} ${styles.groupMembers}`}>
      <summary><span>组内成员</span><strong>{members.length} 人</strong><small>点击查看</small></summary>
      {members.length ? <ul>{members.map((member) => <li key={member.account_id}><span aria-hidden="true">{member.display_name.trim().charAt(0).toUpperCase()}</span><div><strong>{member.display_name}</strong><small>登录名：{member.login_name}</small></div><em data-enabled={member.enabled}>{member.enabled ? "正常" : "已停用"}</em><Button size="sm" kind="danger--ghost" disabled={busy || protectedAccount(member)} onClick={() => onRemoveMember(member)}>移出</Button></li>)}</ul> : <div className={styles.emptyGroupMembers}>这个权限组暂时没有成员</div>}
    </details> : isMember ? <div className={styles.ownMembership}><span>你已加入此权限组</span><Button size="sm" kind="danger--ghost" disabled={busy} onClick={onLeave}>退出权限组</Button></div> : null}
    <details className={styles.groupDisclosure}>
      <summary><span>权限详情</span><strong>{group.permissions.length} 项</strong><small>点击查看</small></summary>
      <div className={styles.groupPermissionDetails}><PermissionGroupSummary permissions={group.permissions} nodes={nodes} /></div>
    </details>
    {canManage ? <div className={styles.rowActions}><Button size="sm" kind="ghost" onClick={onEdit}>编辑</Button><Button size="sm" kind="danger--ghost" disabled={busy} onClick={onDelete}>删除</Button></div> : null}
  </article>;
}
function LevelSelect({ id, label, levels, value, disabled, onChange }: { id: string; label: string; levels: PermissionLevelDefinition[]; value: string; disabled: boolean; onChange: (value: string) => void }) { return <Select id={id} labelText={label} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>{levels.map((item) => <SelectItem key={item.key} value={String(item.level)} text={`${item.level} · ${item.label}`} />)}</Select>; }

function PermissionNodeSections({ nodes, selected, disabled, onToggle }: {
  nodes: PermissionNodeDefinition[];
  selected: string[];
  disabled: boolean;
  onToggle: (key: string) => void;
}) {
  const visibleNodes = nodes.filter((node) => node.custom_assignable || selected.includes(node.key));
  const classified = new Set<string>(PERMISSION_SECTIONS.flatMap((section) => [...section.permissions]));
  const sections = [
    ...PERMISSION_SECTIONS.map((section) => ({
      ...section,
      nodes: visibleNodes.filter((node) => (section.permissions as readonly string[]).includes(node.key)),
    })),
    {
      key: "extensions",
      title: "扩展权限",
      description: "由其他权限插件提供的附加功能。",
      nodes: visibleNodes.filter((node) => !classified.has(node.key)),
    },
  ].filter((section) => section.nodes.length);

  return <div className={styles.permissionSections}>{sections.map((section) => (
    <section className={styles.permissionSection} key={section.key}>
      <header><div><h4>{section.title}</h4><p>{section.description}</p></div><span>{section.nodes.length} 项</span></header>
      <div className={styles.nodeGrid}>{section.nodes.map((node) => {
        const presentation = permissionPresentation(node.key, nodes);
        return <label className={styles.nodeChoice} data-available={node.available} key={node.key}>
          <input type="checkbox" checked={selected.includes(node.key)} disabled={disabled || !node.available} onChange={() => onToggle(node.key)} />
          <span><strong>{presentation.label}</strong><small>{presentation.description}</small></span>
          {!node.available ? <em>权限不可用</em> : null}
        </label>;
      })}</div>
    </section>
  ))}</div>;
}

function PermissionGroupSummary({ permissions, nodes }: { permissions: string[]; nodes: PermissionNodeDefinition[] }) {
  const known = new Set<string>(PERMISSION_SECTIONS.flatMap((section) => [...section.permissions]));
  const sections = [
    ...PERMISSION_SECTIONS.map((section) => ({
      key: section.key,
      title: section.title,
      permissions: permissions.filter((permission) => (section.permissions as readonly string[]).includes(permission)),
    })),
    {
      key: "extensions",
      title: "扩展权限",
      permissions: permissions.filter((permission) => !known.has(permission)),
    },
  ].filter((section) => section.permissions.length);

  return <div className={styles.groupPermissionSummary}>{sections.map((section) => (
    <div className={styles.groupPermissionRow} key={section.key}>
      <strong>{section.title}</strong>
      <div className={styles.chips}>{section.permissions.map((permission) => {
        const presentation = permissionPresentation(permission, nodes);
        return <span data-available={presentation.node?.available ?? false} key={permission}>{presentation.label}{presentation.node?.available === false || !presentation.node ? " · 不可用" : ""}</span>;
      })}</div>
    </div>
  ))}</div>;
}

function KnowledgeDomainCard({ user, boundAccounts, workspaces, levels, busy, canLeave, onManageDomain, onRenameDomain, onDeleteDomain, onLeaveDomain, onCreateWorkspace, onManageWorkspace, onRenameWorkspace, onDeleteWorkspace, onLeaveWorkspace }: {
  user: WebUiWorkspaceUser;
  boundAccounts: AdminAccount[];
  workspaces: WebUiWorkspace[];
  levels: PermissionLevelDefinition[];
  busy: boolean;
  canLeave: boolean;
  onManageDomain: () => void;
  onRenameDomain: () => void;
  onDeleteDomain: () => void;
  onLeaveDomain: () => void;
  onCreateWorkspace: () => void;
  onManageWorkspace: (workspace: WebUiWorkspace) => void;
  onRenameWorkspace: (workspace: WebUiWorkspace) => void;
  onDeleteWorkspace: (workspace: WebUiWorkspace) => void;
  onLeaveWorkspace: (workspace: WebUiWorkspace) => void;
}) {
  return <article className={styles.userCard}>
    <header className={styles.domainHeader}>
      <div className={styles.domainTitle}>
        <span>知识域</span>
        <h3>{user.user_name}</h3>
        {boundAccounts.length ? <p>绑定账号：{boundAccounts.map((item) => item.display_name).join("、")}</p> : null}
      </div>
      <div className={styles.domainHeaderActions}>
        <strong>{workspaces.length} 个知识库</strong>
        {user.can_manage_policy ? <Button size="sm" kind="ghost" renderIcon={Settings} onClick={onManageDomain}>权限设置</Button> : null}
        {user.can_rename ? <Button size="sm" kind="ghost" renderIcon={Edit} disabled={busy} onClick={onRenameDomain}>改名</Button> : null}
        {user.can_delete ? <Button size="sm" kind="danger--ghost" renderIcon={TrashCan} disabled={busy} onClick={onDeleteDomain}>删除知识域</Button> : null}
        {canLeave ? <Button size="sm" kind="danger--ghost" disabled={busy} onClick={onLeaveDomain}>退出知识域</Button> : null}
        {user.can_create_workspace ? <Button size="sm" renderIcon={Add} onClick={onCreateWorkspace}>新建知识库</Button> : <span className={styles.permissionHint}>没有新建权限</span>}
      </div>
    </header>

    <div className={styles.domainAccess}>
      <div><span>允许查看</span><strong>{levelLabel(user.read_min_level, levels)}</strong></div>
      <div><span>允许新建知识库</span><strong>{levelLabel(user.workspace_create_min_level, levels)}</strong></div>
      <TechnicalDetails items={[{ label: "知识域编号", field: "users.user_id", value: user.user_id }]} />
    </div>

    <section className={styles.workspaceSection}>
      <div className={styles.workspaceSectionHeading}><h4>知识库</h4><span>{workspaces.length ? `共 ${workspaces.length} 个` : "尚未创建"}</span></div>
      {workspaces.length ? <div className={styles.workspaceGrid}>{workspaces.map((item) => (
        <article className={styles.workspaceItem} key={item.workspace_id}>
          <header><div><h5>{item.workspace_name}</h5><p>{item.resource_count} 项内容</p></div><span>{item.capabilities.can_add_resource ? "可维护" : "仅查看"}</span></header>
          <div className={styles.workspaceAccess}><span>{levelLabel(item.read_min_level, levels)}可查看</span><span>{levelLabel(item.cud_min_level, levels)}可维护</span></div>
          <TechnicalDetails items={[{ label: "知识库编号", field: "workspace_id", value: item.workspace_id }]} />
          <div className={styles.rowActions}>{item.capabilities.can_manage_policy ? <Button size="sm" kind="ghost" renderIcon={Settings} onClick={() => onManageWorkspace(item)}>权限设置</Button> : null}{item.capabilities.can_rename ? <Button size="sm" kind="ghost" renderIcon={Edit} disabled={busy} onClick={() => onRenameWorkspace(item)}>改名</Button> : null}{item.capabilities.can_delete_workspace ? <Button size="sm" kind="danger--ghost" renderIcon={TrashCan} disabled={busy} onClick={() => onDeleteWorkspace(item)}>删除</Button> : null}{canLeave ? <Button size="sm" kind="danger--ghost" disabled={busy} onClick={() => onLeaveWorkspace(item)}>退出知识库</Button> : null}</div>
        </article>
      ))}</div> : <p className={styles.emptyList}>这个知识域还没有知识库，可以从右上角开始新建。</p>}
    </section>
  </article>;
}

function AccountCards({ accounts, groups, users, levels, canManage, canReset, onEdit, onReset, onBinding }: { accounts: AdminAccount[]; groups: PermissionGroupDefinition[]; users: WebUiWorkspaceUser[]; levels: PermissionLevelDefinition[]; canManage: boolean; canReset: boolean; onEdit: (item: AdminAccount) => void; onReset: (item: AdminAccount) => void; onBinding: (item: AdminAccount) => void }) {
  return <div className={styles.accountGrid}>{accounts.map((item) => {
    const locked = protectedAccount(item);
    const update = !locked && (item.capabilities?.can_update ?? canManage);
    const reset = !locked && (item.capabilities?.can_reset_password ?? canReset);
    const bind = !locked && (item.capabilities?.can_bind_user ?? false);
    const boundDomains = users.filter((user) => item.bound_user_ids.includes(user.user_id));
    const status = locked ? "超级管理员" : item.enabled ? item.must_change_password ? "等待首次改密" : "正常使用" : "已停用";
    return <article className={styles.accountCard} key={item.account_id}>
      <header className={styles.accountCardHeader}><div className={styles.accountIdentity}><span aria-hidden="true">{item.display_name.trim().charAt(0).toUpperCase()}</span><div><h3>{item.display_name}</h3><p>登录名：{item.login_name}</p></div></div><em data-enabled={item.enabled}>{status}</em></header>
      <div className={styles.accountFacts}><div><span>成员等级</span><strong>{levelLabel(item.permission_level, levels)}</strong></div><div><span>权限组</span><strong>{item.groups.map((key) => groupLabelByKey(key, groups)).join("、") || "尚未分配"}</strong></div></div>
      {!locked ? <div className={styles.accountKnowledge} data-bound={Boolean(boundDomains.length)}><span>绑定知识域</span><strong>{boundDomains.map((user) => user.user_name).join("、") || "未绑定"}</strong><small>普通账号可绑定 0～N 个知识域；同一知识域可绑定多个账号</small></div> : null}
      <TechnicalDetails items={[{ label: "用户编号", field: "account_id", value: item.account_id }, ...(item.bound_user_ids.length ? [{ label: "绑定知识域编号", field: "bound_user_ids", value: item.bound_user_ids.join("、") }] : [])]} />
      <div className={styles.rowActions}>{update ? <Button size="sm" kind="ghost" onClick={() => onEdit(item)}>编辑用户</Button> : null}{bind ? <Button size="sm" kind="ghost" onClick={() => onBinding(item)}>设置绑定</Button> : null}{reset ? <Button size="sm" kind="ghost" onClick={() => onReset(item)}>重置密码</Button> : null}{locked ? <span className={styles.protectedHint}>账号权限受系统保护</span> : null}</div>
    </article>;
  })}</div>;
}

function TechnicalDetails({ items }: { items: Array<{ label: string; field: string; value: string }> }) {
  return <details className={styles.technicalDetails}>
    <summary>技术信息</summary>
    <dl className={styles.technicalList}>{items.map((item) => (
      <div key={item.field}>
        <dt>{item.label}<span>（{item.field}）</span></dt>
        <dd><code>{item.value}</code></dd>
      </div>
    ))}</dl>
  </details>;
}
