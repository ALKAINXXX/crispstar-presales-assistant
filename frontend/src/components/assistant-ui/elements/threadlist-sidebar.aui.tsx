// Adapted default starter sidebar; server-supported New/Trigger/Title only.
import { createContext, useContext } from 'react';
import { Database, MessagesSquare, PlusIcon } from 'lucide-react';
import { AuiIf, ThreadListPrimitive, ThreadListItemPrimitive, useAuiState } from '@assistant-ui/react';
import { Sidebar, SidebarContent, SidebarFooter, SidebarHeader, SidebarRail, useSidebar } from '@/components/ui/sidebar';
const ManagementContext=createContext<{onDelete:(id:string,title:string)=>void;canDelete:(id:string)=>boolean}>({onDelete:()=>{},canDelete:()=>false});
export function ThreadListSidebar({onDelete,onDataManagement,canDelete,dataActive=false}:{onDelete:(id:string,title:string)=>void;onDataManagement:()=>void;canDelete:(id:string)=>boolean;dataActive?:boolean}) {
  const {isMobile,setOpenMobile}=useSidebar();
  return <ManagementContext.Provider value={{onDelete,canDelete}}><Sidebar><SidebarHeader className="aui-sidebar-header border-b p-4"><div className="flex items-center gap-2"><MessagesSquare size={22}/><div><p className="font-semibold">澄星</p><p className="text-xs text-muted-foreground">售前助手</p></div></div></SidebarHeader><SidebarContent className="aui-sidebar-content px-2 pt-3"><ThreadList/></SidebarContent><SidebarRail/><SidebarFooter className="aui-sidebar-footer border-t p-4 text-xs leading-5 text-muted-foreground"><button className="data-management-entry" aria-pressed={dataActive} aria-expanded={dataActive} aria-controls={dataActive ? 'data-management-region' : undefined} onClick={()=>{onDataManagement();if (isMobile) setOpenMobile(false);}}><Database size={16}/>数据管理</button><p>单用户本地项目<br/>功能咨询 · 套餐选择 · 订阅基价</p></SidebarFooter></Sidebar></ManagementContext.Provider>;
}
function ThreadList() {
  return <ThreadListPrimitive.Root className="flex flex-col gap-1"><ThreadListPrimitive.New asChild><button className="session-new mb-3 flex items-center gap-2 rounded-xl px-3 py-2 text-sm hover:bg-muted"><PlusIcon size={17}/>新对话</button></ThreadListPrimitive.New><p className="px-3 py-1 text-xs text-muted-foreground">聊天记录</p><AuiIf condition={s=>s.threads.isLoading}><p className="px-3 py-2 text-sm">正在读取…</p></AuiIf><ThreadListPrimitive.Items components={{ThreadListItem}}/></ThreadListPrimitive.Root>;
}
function ThreadListItem() {
  const {isMobile,setOpenMobile} = useSidebar();
  const item=useAuiState(state=>state.threadListItem),{onDelete,canDelete}=useContext(ManagementContext);
  const id=item.remoteId ?? item.id;
  return <ThreadListItemPrimitive.Root className="session-item hover:bg-muted data-active:bg-muted rounded-xl"><ThreadListItemPrimitive.Trigger className="flex min-w-0 flex-1 px-3 py-3 text-start text-sm" onClick={()=>{if (isMobile) setOpenMobile(false);}}><span className="line-clamp-2 break-words"><ThreadListItemPrimitive.Title fallback="新对话"/></span></ThreadListItemPrimitive.Trigger><details className="object-menu"><summary aria-label={`聊天“${item.title ?? '新对话'}”的菜单`}>⋯</summary><div><button disabled={!canDelete(id)} onClick={event=>{event.currentTarget.closest('details')?.removeAttribute('open');onDelete(id,item.title ?? '这段聊天');}}>删除聊天</button></div></details></ThreadListItemPrimitive.Root>;
}
