GET_ORDER_STATE_OPERATION = "getOrderState"
GET_ORDER_DETAILS_OPERATION = "getOrderDetails"

GET_ORDER_STATE_QUERY = """
query getOrderState($merchantUid: String!, $orderCode: String!) {
  merchant(id: $merchantUid) {
    id
    orderDetail(code: $orderCode) {
      state
      modificationTime
      creationTime
      __typename
    }
    __typename
  }
}
""".strip()

GET_ORDER_DETAILS_QUERY = """
query getOrderDetails(
  $merchantUid: String!
  $orderCode: String!
  $skipCustomerPhone: Boolean! = false
) {
  merchant(id: $merchantUid) {
    id
    orderDetail(code: $orderCode) {
      code
      customer {
        phoneNumber @skip(if: $skipCustomerPhone)
        lastName
        firstName
        __typename
      }
      recipient {
        phoneNumber @skip(if: $skipCustomerPhone)
        lastName
        firstName
        __typename
      }
      state
      status
      preOrder
      modificationTime
      creationTime
      delivery {
        transmissionPlanningDate
        plannedDeliveryDate
        mode
        kdAssembled
        kdTransmittedToCourier
        isReturnedToWarehouse
        isOrderArrived
        assembleDate
        actualDeliveryDate
        plannedPointDeliveryDate
        __typename
      }
      entries {
        totalPrice
        quantity
        product {
          name
          code
          __typename
        }
        merchantProduct {
          barcode
          name
          code
          __typename
        }
        entryId
        __typename
      }
      markers {
        marker
        creationTime
        __typename
      }
      returnRequests {
        code
        completionTime
        internalCode
        status
        __typename
      }
      orderSteps {
        __typename
        ... on SimpleOrderStep {
          actualTime
          step
          plannedTime
          timeoutTime
          __typename
        }
        ... on RangeOrderStep {
          step
          from
          to
          __typename
        }
      }
      warehouse {
        ... on Point {
          name
          kaspiDelivery {
            pickupType
            __typename
          }
          city {
            name
            id
            __typename
          }
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}
""".strip()
