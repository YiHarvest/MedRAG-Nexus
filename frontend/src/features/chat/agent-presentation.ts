const ACTION_TITLES: Record<string, string> = {
  create_knowledge_user: "创建知识域",
  rename_knowledge_user: "重命名知识域",
  create_workspace: "创建知识库",
  rename_workspace: "重命名知识库",
  add_text_resource: "添加文本资料",
  request_file_upload: "上传文件",
  delete_file: "删除文件",
  delete_text_resource: "删除文本资料",
  delete_workspace: "删除知识库",
  delete_knowledge_user: "删除知识域",
  change_own_password: "修改个人密码",
  revoke_artifact: "撤销下载文件",
  create_account: "创建账号",
  update_account: "修改账号",
  reset_account_password: "重置账号密码",
  bind_account_to_user: "绑定账号与知识域",
  create_permission_group: "创建权限组",
  update_permission_group: "修改权限组",
  delete_permission_group: "删除权限组",
  leave_own_permission_group: "退出权限组",
  update_user_policy: "修改知识域权限",
  update_workspace_policy: "修改知识库权限",
  replace_user_bindings: "修改知识域授权",
  replace_workspace_bindings: "修改知识库授权",
};

export function agentActionTitle(toolName?: string, fallback?: string): string {
  return (toolName ? ACTION_TITLES[toolName] : undefined) ?? fallback ?? "需要确认操作";
}
