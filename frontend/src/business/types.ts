export interface Requirement { requirement_id: string; statement: string; user_quote: string; feature_id: string | null; kind: string }
export interface Requirements { requirement_id: string; revision: number; items: Requirement[]; selected_plan_id: string | null; parameters: { name: string; value: number; user_quote: string }[]; condition_assertions: { condition_id: string; value: string; user_quote: string }[]; context_quotes?:string[] }
export interface SourceRecord { source_id: string; document_title: string; section_title: string; relative_path: string; line_start: number; line_end: number; dataset_version: string; raw_text: string; [key: string]: unknown }
export interface Evidence { retrieval_id: string; query: string; status: string; hits: {source: SourceRecord; rank: number; score: number; relevance: string}[]; [key: string]: unknown }
export interface MatchItem { requirement_id: string; status: 'supported'|'unsupported'|'unknown'|'pending_condition'; reason_codes: string[]; condition_id: string | null; evidence_source_ids: string[]; condition_evidence_status: string | null; condition_assertion_status: string | null }
export interface Match { requirements: Requirements; plans: {plan_id: string; items: MatchItem[]; satisfies_all: boolean; blocking_reasons: string[]}[]; eligible_plan_ids: string[]; lowest_plan_id: string | null; selected_plan_id: string | null; selection_reason: string; status: string; retrieval_evidence: Evidence[] }
export interface Catalog { version: string; product: {name: string}; plans: {plan_id: string; name: string; rank: number; unit_price_minor: number; features?: {feature_id:string;supported:boolean;condition_id?:string|null}[]}[]; features: {feature_id: string; name: string}[]; conditions: {condition_id: string; name: string; statement: string}[] }
export interface Pricing { plan_id: string; plan_name: string; unit_price_minor: number; paid_seats: number; months: number; base_price_minor: number; formula: string; catalog_version: string }
// Proposal is a read-only server snapshot. It is never assembled from client
// pricing, chat text or remembered response fields, and is not a saved Quote.
export interface Proposal {
  proposal_id: string; session_id: string; view_id?: string|null; turn_id: string; interaction_revision: number; created_at: string;
  catalog_version: string; requirements: Requirements; plans: Match['plans']; eligible_plan_ids: string[];
  presentation?: {product_name:string;plans:{plan_id:string;name:string}[];features:{feature_id:string;name:string}[];purpose:'internal_draft';amount_basis:'subscription_base_price'};
  lowest_plan_id: string; lowest_plan_name: string; selected_plan_id: string; selected_plan_name: string;
  selection_reason: 'lowest_satisfying'|'user_selected';
  pricing: Pricing & {currency:'CNY';billing_unit:'seat_month';minor_units_per_major:100};
  conditions: {condition_id:string;name:string;statement:string;user_quote:string;source_ids:string[]}[];
  adopted_sources: {retrieval_id:string;chain_id:string;dataset_version:string;index_build_id:string;index_inputs_sha256:string;source:SourceRecord}[];
  action_ref: string; status:'pending_confirmation'; save_available:boolean; notice:string;
}
export type ObjectStatus = 'active'|'trash'|'permanently_deleted'|'not_found';
export interface Quote {quote_id:string;saved_at:string;snapshot_version:1;snapshot:Proposal;source_conversation_status?:ObjectStatus}
export interface QuoteSummary {quote_id:string;saved_at:string;selected_plan_name:string;paid_seats:number;months:number;base_price_minor:number;source_session_id:string;source_conversation_status:ObjectStatus}
export interface QuoteFacts {quote_id:string;saved_at:string;snapshot_version:1;snapshot:Omit<Proposal,'proposal_id'|'session_id'|'turn_id'|'view_id'|'interaction_revision'|'action_ref'|'status'|'save_available'|'notice'>}
export interface QuoteActionResult {status:'saved'|'not_saved'|'unknown';code:string;message:string;quote:Quote|null;quote_id?:string;current_status?:ObjectStatus;source_conversation_status?:ObjectStatus}
// Operation references belong only to the browser controller and ordinary
// business endpoints. They never enter model messages or rendered diagnostics.
export interface SaveAction {session_id:string;action_ref:string;proposal_id:string;started_at:string;status:'submitting'|'checking'|'saved'|'not_saved'|'unknown';message:string;code:string;quote:Quote|null;restored?:boolean;quote_id?:string;current_status?:ObjectStatus;source_conversation_status?:ObjectStatus}
export interface CurrentTask { kind: 'selection'; status: 'current'|'needs_reassessment'; last_result: 'updated'|'not_updated'; result_turn_id?: string | null; result_message_id?: string | number | null; requirements: Requirements; match: Match | null; pricing: Pricing | null; pending: string[]; questions: string[] }
export interface OperationFeedback {group_id:string;status:'accepted'|'rejected'|'blocked';target?:string;field?:string;issue?:string|null;message:string;previous_value?:number|string|null;retained?:boolean}
export interface SemanticReviewMetrics {status:string;dispatched?:number;elapsed_ms?:number;usage?:Record<string,number>|null;error_code?:string|null;[key:string]:unknown}
export interface Business { turn_id: string; intent: string; requirements: Requirements; match: Match | null; pricing: Pricing | null; unit_price: Partial<Pricing> | null; current_task: CurrentTask | null; catalog_context: Catalog | null; catalog: Catalog | null; retrieval_evidence: Evidence[]; knowledge_answers: {retrieval_id: string; citations: {source_id: string; quote: string}[]; [key: string]: unknown}[]; errors: Record<string, unknown>[]; changes: string[]; questions: string[]; pending: string[]; recovery_notice?: string | null; proposal?: Proposal | null; queried_quotes?:QuoteFacts[]; semantic_review?:SemanticReviewMetrics|null; operation_feedback?:OperationFeedback[]; presentation?: {answer_markdown: string; show_comparison: boolean; show_current_result: boolean; used_source_ids: string[];feedback?:OperationFeedback[]} }
export interface ChatResponse { message: string; message_id?: string | number; session_id: string; status: string; business: Business | null; tool_results: Record<string, unknown>[]; model?: string;session_version?:number }
export interface ViewMessage { id: string; role: 'user'|'assistant'; content: string; historical?: boolean; response?: ChatResponse; createdAt: Date }
export interface Session { id: string; title: string; updated_at: string;version?:number;last_message_id?:string|number|null }
export interface SessionRead { messages: {id?: string | number; role: string; content: string}[]; current_task?: CurrentTask | null; catalog_context?: Catalog | null; business_state_available?: boolean; recovery_notice?: string | null;session_version?:number }
export type ManagementOperation = 'trash'|'restore'|'purge'|'empty_trash'|'clear';
export interface TrashItem {object_type:'chat'|'quote';object_id:string;title:string;deleted_at:string;saved_at?:string;source_session_id?:string;source_conversation_status?:ObjectStatus}
export interface DataManagement {dataset_id:string;location_id:string;epoch:string;revision:number;database_path:string;counts:{conversations:number;messages:number;quotes:number;trash_conversations:number;trash_quotes:number;trash_messages:number};last_clear?:{action_ref:string;completed_at:string}|null;clearing?:boolean}
export interface ManagementAction {action_ref:string;operation:ManagementOperation;object_type:'chat'|'quote'|null;object_id:string|null;expected_revision:number;status:'submitting'|'unknown'|'completed'|'not_completed';message:string;code?:string;completed_count?:number|null;remaining_count?:number|null;dataset_id?:string;epoch?:string;revision?:number}
