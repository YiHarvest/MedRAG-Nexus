"use client";

import { ArrowLeft } from "@carbon/icons-react";
import { Button } from "@carbon/react";

export default function NotFound() {
  return (
    <div className="page not-found">
      <p className="not-found-code">404</p>
      <p className="page-eyebrow"><span aria-hidden="true" />Route not found</p>
      <h1 className="page-title">这条知识路径不存在</h1>
      <p className="page-description">地址可能已失效，返回文档中心继续管理知识空间。</p>
      <Button href="/documents" renderIcon={ArrowLeft}>返回文档中心</Button>
    </div>
  );
}
