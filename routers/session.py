# FILE: routers/session.py

from fastapi import APIRouter, HTTPException, Depends, Form, status, Query, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from typing import List
import httpx

# --- Database Connection Import ---
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db_connection

# =============================================================================
# CONFIGURATION
# =============================================================================
AUTH_SERVICE_ME_URL = "http://localhost:4000/auth/users/me"
BLOCKCHAIN_LOG_URL = "http://localhost:9005/blockchain/log"

# =============================================================================
# ROUTER SETUP & OAUTH2 SCHEME
# =============================================================================
router = APIRouter(prefix="/session", tags=["Cashier Sessions"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://localhost:4000/auth/token")

# =============================================================================
# AUTHORIZATION HELPER
# =============================================================================
async def validate_token_and_roles(token: str, allowed_roles: List[str]):
    """
    Validates the bearer token against the auth service and checks user role.
    """
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(AUTH_SERVICE_ME_URL, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Authentication service error: {e.response.text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Authentication service is unavailable: {e}")

    user_data = response.json()
    user_role = user_data.get("userRole")

    if user_role not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Access denied. Role '{user_role}' is not authorized for this action.")
    
    user_data['access_token'] = token  # Store token for blockchain logging
    return user_data

# =============================================================================
# BLOCKCHAIN LOGGING HELPER
# =============================================================================
async def log_to_blockchain(
    service_identifier: str,
    action: str,
    entity_type: str,
    entity_id: int,
    actor_username: str,
    change_description: str,
    data: dict,
    token: str
):
    """Log activity to blockchain service (non-blocking)"""
    import logging
    logger = logging.getLogger(__name__)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                BLOCKCHAIN_LOG_URL,
                json={
                    "service_identifier": service_identifier,
                    "action": action,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "actor_username": actor_username,
                    "change_description": change_description,
                    "data": data
                },
                headers={"Authorization": f"Bearer {token}"}
            )
            
            if response.status_code == 201:
                logger.info(f"✅ Blockchain log created: {action} - {entity_type} #{entity_id}")
            else:
                logger.warning(f"⚠️ Blockchain logging failed: {response.status_code} - {response.text}")
                
        except Exception as e:
            logger.error(f"❌ Blockchain logging error: {e}")
            # Don't raise exception - blockchain logging failure shouldn't block main operation

# =============================================================================
# SESSION ENDPOINTS
# =============================================================================

@router.get('/status')
async def get_session_status(
    token: str = Depends(oauth2_scheme),
    cashier_name: str = Query(..., description="The name of the cashier to check the session status for.")
):
    """
    Checks if a specific cashier has an active session.
    """
    await validate_token_and_roles(token, allowed_roles=["manager", "admin", "cashier"])

    conn, cursor = None, None
    try:
        conn = await get_db_connection()
        cursor = await conn.cursor()
        
        await cursor.execute(
            """
            SELECT SessionID, InitialCash, SessionStart 
            FROM CashierSessions 
            WHERE CashierName = ? AND Status = 'Active'
            """,
            (cashier_name,)
        )
        active_session = await cursor.fetchone()

        if active_session:
            return {
                "hasActiveSession": True,
                "cashierName": cashier_name,
                "sessionId": active_session[0],
                "initialCash": active_session[1],
                "sessionStart": active_session[2].isoformat()
            }
        else:
            return {"hasActiveSession": False, "cashierName": cashier_name}
            
    except Exception as e:
        print(f"Error in get_session_status: {e}")
        raise HTTPException(status_code=500, detail="Failed to check cashier session status.")
    finally:
        if cursor: await cursor.close()
        if conn: await conn.close()


@router.post('/start', status_code=status.HTTP_201_CREATED)
async def start_session(
    initial_cash: float = Form(...),
    background_tasks: BackgroundTasks = None,
    token: str = Depends(oauth2_scheme)
):
    """
    Starts a new cashier session for the currently authenticated user.
    """
    user_data = await validate_token_and_roles(token, allowed_roles=["cashier", "manager", "admin"])
    cashier_name = user_data.get("username")

    if not cashier_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username not found in authentication token.")
        
    if initial_cash < 0:
        raise HTTPException(status_code=400, detail="Initial cash amount cannot be negative.")

    conn, cursor = None, None
    session_id = None
    
    try:
        conn = await get_db_connection()
        cursor = await conn.cursor()

        await cursor.execute(
            "SELECT 1 FROM CashierSessions WHERE CashierName = ? AND Status = 'Active'",
            (cashier_name,)
        )
        if await cursor.fetchone():
            raise HTTPException(status_code=409, detail="An active session already exists for this cashier.")

        # Insert and get the new session ID
        await cursor.execute(
            "INSERT INTO CashierSessions (CashierName, InitialCash) VALUES (?, ?)",
            (cashier_name, initial_cash)
        )
        
        # Get the newly created session ID using a separate query
        await cursor.execute("SELECT @@IDENTITY")
        result = await cursor.fetchone()
        session_id = int(result[0]) if result and result[0] else None
        
        await conn.commit()

        # Prepare blockchain log data
        blockchain_data = {
            "sessionId": session_id,
            "cashierName": cashier_name,
            "initialCash": initial_cash,
            "status": "Active"
        }
        
        # Log to blockchain in background if BackgroundTasks is available
        if background_tasks and session_id:
            background_tasks.add_task(
                log_to_blockchain,
                service_identifier="CASHIER_SESSION",
                action="CREATE",
                entity_type="CashierSession",
                entity_id=session_id,
                actor_username=cashier_name,
                change_description=f"New cashier session started with initial cash: {initial_cash}",
                data=blockchain_data,
                token=user_data.get('access_token')
            )

        return {
            "message": f"Cashier session for '{cashier_name}' started successfully.",
            "sessionId": session_id,
            "initialCash": initial_cash
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in start_session: {e}")
        raise HTTPException(status_code=500, detail="Failed to start cashier session.")
    finally:
        if cursor: await cursor.close()
        if conn: await conn.close()

@router.get('/check-closed-today')
async def check_closed_session_today(
    token: str = Depends(oauth2_scheme),
    cashier_name: str = Query(..., description="The cashier's username")
):
    """
    Checks if a cashier has already closed a session today.
    """
    await validate_token_and_roles(token, allowed_roles=["manager", "admin", "cashier"])

    conn, cursor = None, None
    try:
        conn = await get_db_connection()
        cursor = await conn.cursor()
        
        # Check for closed sessions today
        await cursor.execute(
            """
            SELECT TOP 1 SessionID, SessionEnd 
            FROM CashierSessions 
            WHERE CashierName = ? 
              AND Status = 'Closed'
              AND CAST(SessionEnd AS DATE) = CAST(GETDATE() AS DATE)
            ORDER BY SessionEnd DESC
            """,
            (cashier_name,)
        )
        closed_session = await cursor.fetchone()

        if closed_session:
            return {
                "hasClosedSessionToday": True,
                "sessionId": closed_session[0],
                "sessionEnd": closed_session[1].isoformat() if closed_session[1] else None
            }
        else:
            return {
                "hasClosedSessionToday": False
            }
            
    except Exception as e:
        print(f"Error checking closed session: {e}")
        raise HTTPException(status_code=500, detail="Failed to check closed session status.")
    finally:
        if cursor: await cursor.close()
        if conn: await conn.close()


@router.get('/summary')
async def get_session_summary(
    token: str = Depends(oauth2_scheme),
    cashier_name: str = Query(..., description="The name of the cashier")
):
    """
    Retrieves the summary of the most recent closed session for the cashier today.
    """
    await validate_token_and_roles(token, allowed_roles=["manager", "admin", "cashier"])

    conn, cursor = None, None
    try:
        conn = await get_db_connection()
        cursor = await conn.cursor()

        # 1. Fetch the session details (including CheckedBy for manager name)
        await cursor.execute(
            """
            SELECT TOP 1 
                SessionID, 
                InitialCash, 
                SessionStart, 
                SessionEnd, 
                ClosingCash,
                CashSalesAtClose,
                CheckedBy
            FROM CashierSessions 
            WHERE CashierName = ? 
              AND Status = 'Closed'
              AND CAST(SessionEnd AS DATE) = CAST(GETDATE() AS DATE)
            ORDER BY SessionEnd DESC
            """,
            (cashier_name,)
        )
        session_row = await cursor.fetchone()

        if not session_row:
            raise HTTPException(status_code=404, detail="No closed session found for today.")

        session_id, initial_cash, start_time, end_time, closing_cash, cash_sales_at_close, checked_by = session_row

        # 2. Get full names for cashier and manager
        cashier_full_name = cashier_name
        manager_full_name = "N/A"
        
        async with httpx.AsyncClient() as client:
            # Get cashier full name
            try:
                cashier_response = await client.get(
                    f"http://localhost:4000/users/employee_name?username={cashier_name}",
                    headers={"Authorization": f"Bearer {token}"}
                )
                if cashier_response.status_code == 200:
                    cashier_data = cashier_response.json()
                    cashier_full_name = cashier_data.get("employee_name", cashier_name)
            except Exception as e:
                print(f"Error fetching cashier full name: {e}")
            
            # Get manager full name if CheckedBy exists
            if checked_by:
                try:
                    manager_response = await client.get(
                        f"http://localhost:4000/users/employee_name?username={checked_by}",
                        headers={"Authorization": f"Bearer {token}"}
                    )
                    if manager_response.status_code == 200:
                        manager_data = manager_response.json()
                        manager_full_name = manager_data.get("employee_name", checked_by)
                except Exception as e:
                    print(f"Error fetching manager full name: {e}")

        # 3. Fetch transaction counts linked to this session
        await cursor.execute(
            """
            SELECT 
                COUNT(*) as Total,
                SUM(CASE WHEN PaymentMethod = 'Cash' THEN 1 ELSE 0 END) as CashTx,
                SUM(CASE WHEN PaymentMethod != 'Cash' THEN 1 ELSE 0 END) as NonCashTx,
                SUM(CASE WHEN Status = 'void' THEN 1 ELSE 0 END) as VoidTx
            FROM Sales
            WHERE SessionID = ?
            """,
            (session_id,)
        )
        tx_stats = await cursor.fetchone()
        
        total_tx = tx_stats[0] or 0
        cash_tx = tx_stats[1] or 0
        card_tx = tx_stats[2] or 0
        void_tx = tx_stats[3] or 0

        # 4. Calculate Total Sales Amount (Gross - Discounts)
        # Note: We use CashSalesAtClose from session for cash part, but for Total Sales display
        # we calculate based on completed sales.
        await cursor.execute(
            """
            SELECT SUM(FinalAmount) FROM (
                SELECT (SUM(si.Quantity * si.UnitPrice) - MIN(s.TotalDiscountAmount) - MIN(s.PromotionalDiscountAmount)) as FinalAmount
                FROM Sales s
                JOIN SaleItems si ON s.SaleID = si.SaleID
                WHERE s.SessionID = ? AND s.Status = 'completed'
                GROUP BY s.SaleID
            ) as SessionSales
            """, 
            (session_id,)
        )
        total_sales_row = await cursor.fetchone()
        total_sales_calculated = total_sales_row[0] if total_sales_row and total_sales_row[0] else 0.0

        # 5. Calculate Expected Cash
        # Expected = Initial + Cash Sales
        expected_cash = float(initial_cash) + float(cash_sales_at_close or 0)

        return {
            "cashier_name": cashier_full_name,
            "manager_name": manager_full_name,
            "date": start_time.strftime("%Y-%m-%d"),
            "start_time": start_time.strftime("%I:%M %p"),
            "end_time": end_time.strftime("%I:%M %p") if end_time else "N/A",
            "initial_cash": float(initial_cash),
            "total_sales": float(total_sales_calculated),
            "cash_in_drawer": float(closing_cash) if closing_cash is not None else 0.0,
            "expected_cash": float(expected_cash),
            "total_transactions": total_tx,
            "cash_transactions": cash_tx,
            "card_transactions": card_tx,
            "void_transactions": void_tx
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting session summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve session summary.")
    finally:
        if cursor: await cursor.close()
        if conn: await conn.close()