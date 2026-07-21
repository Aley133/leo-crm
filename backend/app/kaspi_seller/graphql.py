GET_ORDER_STATE_OPERATION = "getOrderState"
GET_ORDER_DETAILS_OPERATION = "getOrderDetails"

GET_ORDER_STATE_QUERY = """
query getOrderState($merchantId: String!, $orderCode: String!) {
  merchant(id: $merchantId) {
    id
    orderDetail(code: $orderCode) {
      state
      status
      modificationTime
      creationTime
    }
  }
}
""".strip()

GET_ORDER_DETAILS_QUERY = """
query getOrderDetails($merchantId: String!, $orderCode: String!) {
  merchant(id: $merchantId) {
    id
    orderDetail(code: $orderCode) {
      code
      state
      status
      preOrder
      modificationTime
      delivery {
        isOrderArrived
        kdAssembled
        kdTransmittedToCourier
        assembleDate
        transmissionPlanningDate
      }
      markers {
        marker
        creationTime
      }
      orderSteps {
        step
        actualTime
        plannedTime
        timeoutTime
      }
    }
  }
}
""".strip()
