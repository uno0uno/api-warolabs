# Database Governance Rules - WaroLabs API

## Core Governance Principles

### Rule 1: Database First
- Always query the database before any implementation
- Never assume or infer data structure
- All information must be verified against actual database state

### Rule 2: MCP Primary Source
- Use MCP warolabs connection for all data verification
- Database is the single source of truth
- No exceptions to database consultation requirement

### Rule 3: Zero Assumptions
- Do not assume column names, types, or relationships
- Verify all table structures before proceeding
- Check actual data patterns and constraints

### Rule 4: Tenant Isolation Mandatory
- Always verify tenant_id exists and is properly filtered
- Check tenant-specific data access patterns
- Validate isolation implementation in all operations

### Rule 5: Schema Verification Required
- Query information_schema before implementation
- Verify table existence and structure
- Check foreign key relationships and constraints

### Rule 6: Data Pattern Analysis
- Examine existing data before creating new structures
- Understand current usage patterns
- Respect established data conventions

### Rule 7: Implementation Follows Reality
- Code must reflect actual database structure
- No deviation from verified schema
- Models must match database exactly

### Rule 8: Documentation Accuracy
- Update documentation when discrepancies found
- Database truth overrides written documentation
- Keep governance rules current

### Rule 9: Quality Gates
- No implementation without database verification
- All assumptions must be tested and proven
- Code review must verify database consultation

### Rule 10: Compliance Monitoring
- Regular audits of implementation vs database
- Enforcement of governance rules in all processes
- Zero tolerance for assumption-based development

---

**Authority:** Database schema and actual data  
**Enforcement:** Mandatory for all development  
**Exceptions:** None permitted